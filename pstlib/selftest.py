"""Self-test: builds synthetic PST files, reads them back, exports every format.

    python pst_exporter.py selftest            synthetic files only
    python pst_exporter.py selftest FILE.pst   also open a real file and export it

Writes <app folder>/pst-exporter-selftest.txt and returns 0 when everything
passed. A build script treats "PROBLEMS FOUND" in that file as a failure.
"""
from __future__ import annotations

import datetime as _dt
import email
import glob
import os
import shutil
import tempfile
import traceback
from email import policy as _policy
from typing import List, Optional

from . import mapi, rtf, synth
from .export import ExportOptions, FORMATS, export, sanitize, unique_path
from .message import PSTFile
from .ndb import CRYPT_CYCLIC, CRYPT_NONE, CRYPT_PERMUTE, decrypt_cyclic, decrypt_permute
from .paths import APP, VERSION, app_dir

REPORT_NAME = "pst-exporter-selftest.txt"

_LONG_BODY = ("Paragraph %d of a deliberately long body, long enough to spill out of the heap into a sub-node "
              "and across more than one 8 KB data block, so the XBLOCK path gets exercised too.\n")
LONG_BODY = "".join(_LONG_BODY % i for i in range(160))     # ~24 KB -> 3+ blocks as UTF-16
UNICODE_SUBJECT = "Prüfung – naïve café ☕ 日本語 Ελληνικά"
SAMPLE_RTF = (b"{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl{\\f0\\fswiss Arial;}}\\pard Hello from \\b RTF\\b0  "
              b"with caf\\'e9 and \\u8364? euro.\\par Second line.\\par}")
SAMPLE_RTF_HTML = (b"{\\rtf1\\ansi\\ansicpg1252\\fromhtml1\\deff0{\\fonttbl{\\f0\\fswiss Arial;}}"
                   b"{\\*\\htmltag1 <html>}{\\*\\htmltag2 <body>}{\\*\\htmltag96 <p>}"
                   b"\\htmlrtf \\pard\\plain \\htmlrtf0 Encapsulated \\htmlrtf\\b\\htmlrtf0 "
                   b"{\\*\\htmltag84 <b>}bold{\\*\\htmltag92 </b>}\\htmlrtf\\b0\\htmlrtf0  text"
                   b"{\\*\\htmltag104 </p>}{\\*\\htmltag3 </body>}{\\*\\htmltag4 </html>}}")


def _lzfu_compress_stored(rtf_bytes: bytes) -> bytes:
    """MELA (stored) form of compressed RTF - the reader must accept it."""
    import struct
    return struct.pack("<IIII", len(rtf_bytes) + 12, len(rtf_bytes), 0x414C454D, 0) + rtf_bytes


def build_mailbox() -> List[synth.SynthFolder]:
    when = _dt.datetime(2024, 3, 5, 9, 30, 15, tzinfo=_dt.timezone.utc)
    png = (b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4)
    big_attachment = bytes((i * 7) & 0xFF for i in range(50_000))    # 50 KB -> XBLOCK
    embedded = synth.SynthMessage("Forwarded original", "Original Sender", "orig@example.org",
                                  [("Someone", "someone@example.org")], when - _dt.timedelta(days=3),
                                  body="This is the embedded message body.")
    inbox_msgs = [
        synth.SynthMessage("Plain text message", "Alice Example", "alice@example.com",
                           [("Bob Example", "bob@example.com")], when, body="Hello Bob,\n\nJust a plain text note.\n\nAlice",
                           headers="Received: from mail.example.com by mx.test; Tue, 5 Mar 2024 09:30:00 +0000\r\n"
                                   "X-Mailer: Synthetic 1.0\r\nThread-Topic: Plain text message\r\n"),
        synth.SynthMessage(UNICODE_SUBJECT, "Zoë Ünïcode", "zoe@example.com",
                           [("Bob Example", "bob@example.com"), ("Carol", "carol@example.com")],
                           when + _dt.timedelta(hours=1), body="Unicode body: naïve café ☕ 日本語 — ok",
                           html="<html><body><p>Unicode <b>HTML</b> body: naïve café ☕ 日本語</p>"
                                "<img src=\"cid:pic1\"></body></html>",
                           attachments=[("picture.png", png, "image/png", "pic1")],
                           extra={mapi.PR_IMPORTANCE: (mapi.PT_LONG, 2)}, read=False,
                           cc=[("Dave", "dave@example.com")]),
        synth.SynthMessage("Long body and a big attachment", "Alice Example", "alice@example.com",
                           [("Bob Example", "bob@example.com")], when + _dt.timedelta(hours=2), body=LONG_BODY,
                           attachments=[("big file.bin", big_attachment, "application/octet-stream"),
                                        ("notes.txt", b"attached text\n", "text/plain")]),
        synth.SynthMessage("RTF only body", "Alice Example", "alice@example.com", [("Bob Example", "bob@example.com")],
                           when + _dt.timedelta(hours=3), rtf=_lzfu_compress_stored(SAMPLE_RTF)),
        synth.SynthMessage("RTF with encapsulated HTML", "Alice Example", "alice@example.com",
                           [("Bob Example", "bob@example.com")], when + _dt.timedelta(hours=4),
                           rtf=_lzfu_compress_stored(SAMPLE_RTF_HTML)),
        synth.SynthMessage("FW: with an embedded message", "Alice Example", "alice@example.com",
                           [("Bob Example", "bob@example.com")], when + _dt.timedelta(hours=5),
                           body="See attached message.", embedded=embedded),
        synth.SynthMessage("Nasty subject: <>:\"/\\|?* CON", "Alice Example", "alice@example.com",
                           [("Bob Example", "bob@example.com")], when + _dt.timedelta(hours=6), body="Filename safety."),
        synth.SynthMessage("Meeting", "Alice Example", "alice@example.com", [("Bob Example", "bob@example.com")],
                           when + _dt.timedelta(hours=7), body="A calendar item.", message_class="IPM.Appointment"),
    ]
    many = [synth.SynthMessage(f"Bulk message {i:03d}", f"Sender {i % 7}", f"s{i % 7}@example.com",
                               [("Bob Example", "bob@example.com")], when + _dt.timedelta(minutes=i),
                               body=f"Body of bulk message {i}.") for i in range(230)]
    return [synth.SynthFolder("Top of Personal Folders", subfolders=[
        synth.SynthFolder("Inbox", inbox_msgs, subfolders=[synth.SynthFolder("Project X", many[:5])]),
        synth.SynthFolder("Sent Items", [synth.SynthMessage("Sent one", "Bob Example", "bob@example.com",
                                                             [("Alice Example", "alice@example.com")], when, body="Sent body.")]),
        synth.SynthFolder("Bulk", many),
        synth.SynthFolder("Empty"),
    ])]


def run(verbose: bool = True, extra_files: Optional[List[str]] = None) -> int:
    results = []

    def record(name, ok, detail=""):
        results.append((name, "PASS" if ok else "FAIL", detail))
        if verbose:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))

    if verbose:
        print(f"{APP} {VERSION} self-test\n")
    tmp = tempfile.mkdtemp(prefix="pst-exporter-selftest-")
    try:
        # ---- ciphers are exact inverses
        data = bytes(range(256)) * 3
        record("permute cipher round-trip", decrypt_permute(synth.encrypt_permute(data)) == data)
        record("cyclic cipher round-trip", decrypt_cyclic(synth.encrypt_cyclic(data, 0x1234ABCD), 0x1234ABCD) == data)
        # ---- RTF
        t = rtf.rtf_to_text(SAMPLE_RTF)
        record("RTF to text", "Hello from RTF with café and € euro." in t and "Second line." in t, repr(t[:80]))
        h = rtf.rtf_to_html(SAMPLE_RTF_HTML) or ""
        record("RTF HTML de-encapsulation", "<b>bold</b>" in h and "Encapsulated" in h and "\\pard" not in h, repr(h[:100]))
        record("HTML to text", rtf.html_to_text("<p>One&nbsp;two</p><br><div>three</div>") == "One two\n\nthree",
               repr(rtf.html_to_text("<p>One&nbsp;two</p><br><div>three</div>")))
        record("filename sanitising", sanitize('a<b>:"c/d\\e|f?g*h  .') == "a_b___c_d_e_f_g_h" and sanitize("CON") == "_CON")

        # ---- synthetic files, one per encryption mode
        mailbox = build_mailbox()
        for crypt, label in ((CRYPT_NONE, "none"), (CRYPT_PERMUTE, "compressible"), (CRYPT_CYCLIC, "high")):
            path = os.path.join(tmp, f"synthetic-{label}.pst")
            try:
                synth.write_pst(path, mailbox, crypt=crypt)
                _check_synthetic(path, label, record, tmp)
            except Exception as exc:  # noqa: BLE001
                record(f"synthetic PST ({label})", False, f"{type(exc).__name__}: {exc}")
                if verbose:
                    traceback.print_exc()

        # ---- optional real files
        for f in extra_files or []:
            try:
                _check_real(f, record, tmp)
            except Exception as exc:  # noqa: BLE001
                record(f"real file {os.path.basename(f)}", False, f"{type(exc).__name__}: {exc}")
                if verbose:
                    traceback.print_exc()

        # ---- PDF engine present
        try:
            from . import pdfout
            fonts = pdfout.fonts()
            record("PDF fonts", True, f"body={fonts['regular']} unicode={'yes' if fonts['unicode'] else 'no (Latin only)'}")
        except Exception as exc:  # noqa: BLE001
            record("PDF fonts", False, str(exc))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    passed = sum(1 for r in results if r[1] == "PASS")
    failed = [r for r in results if r[1] == "FAIL"]
    lines = [f"{APP} {VERSION} self-test", "", f"{passed} passed, {len(failed)} failed.", ""]
    if failed:
        lines.append("PROBLEMS FOUND:")
        lines += [f"  {n}: {d}" for n, _s, d in failed]
    else:
        lines.append("No problems found.")
    report = "\n".join(lines)
    try:
        with open(os.path.join(app_dir(), REPORT_NAME), "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
    except Exception:  # noqa: BLE001
        pass
    if verbose:
        print("\n" + report)
    return 0 if not failed else 1


def _check_synthetic(path: str, label: str, record, tmp: str):
    pst = PSTFile(path)
    try:
        record(f"open synthetic ({label})", pst.ndb.crypt in (0, 1, 2) and pst.store_name == "Synthetic test store",
               pst.description)
        folders = {f.name: f for f in pst.root.walk()}
        record(f"folder tree ({label})", all(n in folders for n in ("Inbox", "Sent Items", "Bulk", "Empty", "Project X")),
               ", ".join(sorted(folders)))
        inbox = folders["Inbox"]
        rows = inbox.messages()
        record(f"inbox listing ({label})", len(rows) == 8 and rows[1].subject == UNICODE_SUBJECT and not rows[1].read,
               f"{len(rows)} rows")
        bulk = folders["Bulk"].messages()
        record(f"multi-block table rows ({label})", len(bulk) == 230 and bulk[229].subject == "Bulk message 229",
               f"{len(bulk)} rows")
        record(f"empty folder ({label})", folders["Empty"].messages() == [])
        msgs = {r.subject: r.open() for r in rows}
        m = msgs["Plain text message"]
        record(f"plain message ({label})", m.body_text.startswith("Hello Bob") and m.sender == "Alice Example <alice@example.com>"
               and [r.address for r in m.recipients()] == ["bob@example.com"]
               and m.date == _dt.datetime(2024, 3, 5, 9, 30, 15, tzinfo=_dt.timezone.utc)
               and "X-Mailer: Synthetic 1.0" in m.transport_headers)
        m = msgs[UNICODE_SUBJECT]
        atts = m.attachments()
        record(f"unicode + html + inline image ({label})", m.body_text.endswith("— ok") and "<b>HTML</b>" in m.body_html
               and len(atts) == 1 and atts[0].filename == "picture.png" and atts[0].data.startswith(b"\x89PNG")
               and len(atts[0].data) == 8 + 1024 and m.importance == 2 and len(m.recipients_of("Cc")) == 1)
        m = msgs["Long body and a big attachment"]
        atts = m.attachments()
        big = [a for a in atts if a.filename == "big file.bin"][0]
        record(f"XBLOCK data trees ({label})", m.body_text == LONG_BODY and len(big.data) == 50_000
               and big.data[12345] == (12345 * 7) & 0xFF and [a.filename for a in atts] == ["big file.bin", "notes.txt"])
        m = msgs["RTF only body"]
        record(f"RTF-only body ({label})", "Hello from RTF with café and € euro." in m.best_text())
        m = msgs["RTF with encapsulated HTML"]
        record(f"RTF-encapsulated HTML ({label})", "<b>bold</b>" in m.best_html() and "Encapsulated bold text" in m.best_text())
        m = msgs["FW: with an embedded message"]
        atts = m.attachments()
        emb = atts[0].embedded_message if atts else None
        record(f"embedded message ({label})", emb is not None and emb.subject == "Forwarded original"
               and emb.body_text == "This is the embedded message body." and emb.sender_email == "orig@example.org")

        # ---- exports
        out_root = os.path.join(tmp, f"export-{label}")
        all_rows = list(pst.all_message_rows())
        for fmt in FORMATS:
            out = os.path.join(out_root, fmt)
            res = export(all_rows, ExportOptions(fmt=fmt, out_dir=out, pdf_single_file=False))
            ok = res.exported == len(all_rows) and not res.failed and not res.errors
            detail = res.summary() + ("; " + res.errors[0] if res.errors else "")
            if fmt == "eml":
                files = glob.glob(os.path.join(out, "**", "*.eml"), recursive=True)
                ok = ok and len(files) == len(all_rows)
                # parse the unicode one back
                target = [f for f in files if "Prüfung" in os.path.basename(f)]
                if target:
                    with open(target[0], "rb") as fh:
                        em = email.message_from_binary_file(fh, policy=_policy.default)
                    body = em.get_body(preferencelist=("html",))
                    related = [p for p in em.walk() if p.get("Content-ID")]
                    ok = ok and em["Subject"] == UNICODE_SUBJECT and body is not None and "<b>HTML</b>" in body.get_content() \
                        and related and related[0].get_payload(decode=True).startswith(b"\x89PNG") \
                        and em["Importance"] == "High" and "dave@example.com" in (em["Cc"] or "")
                    detail += f"; parsed back {os.path.basename(target[0])}"
                else:
                    ok = False
                    detail += "; unicode EML missing"
                fw = [f for f in files if os.path.basename(f).startswith("2024-03-05 143015 FW")]
                if fw:
                    with open(fw[0], "rb") as fh:
                        em = email.message_from_binary_file(fh, policy=_policy.default)
                    rfc = [p for p in em.walk() if p.get_content_type() == "message/rfc822"]
                    ok = ok and bool(rfc)
                    if rfc:
                        inner = rfc[0].get_payload()[0]
                        ok = ok and inner["Subject"] == "Forwarded original"
                nasty = [f for f in files if "Nasty subject" in os.path.basename(f)]
                ok = ok and bool(nasty) and not any(ch in os.path.basename(nasty[0]) for ch in '<>:"/\\|?*')
            elif fmt == "mbox":
                import mailbox as _mb
                total = 0
                for f in glob.glob(os.path.join(out, "**", "*.mbox"), recursive=True):
                    mb = _mb.mbox(f)
                    total += len(mb)
                    mb.close()
                ok = ok and total == len(all_rows)
                detail += f"; {total} messages in mbox files"
            elif fmt == "pdf":
                files = glob.glob(os.path.join(out, "**", "*.pdf"), recursive=True)
                ok = ok and len(files) == len(all_rows) and all(open(f, "rb").read(5) == b"%PDF-" for f in files[:20])
                # and the combined-PDF mode
                res2 = export(all_rows[:12], ExportOptions(fmt="pdf", out_dir=out + "-single", pdf_single_file=True,
                                                          include_attachments=False))
                single = glob.glob(os.path.join(out + "-single", "*.pdf"))
                ok = ok and len(single) == 1 and res2.exported == 12 and not res2.errors
                detail += "; combined PDF ok" if single else "; combined PDF missing"
            elif fmt == "html":
                files = glob.glob(os.path.join(out, "**", "*.html"), recursive=True)
                target = [f for f in files if "Prüfung" in os.path.basename(f)]
                ok = ok and len(files) == len(all_rows) and target and "data:image/png;base64," in open(target[0], encoding="utf-8").read()
            elif fmt == "txt":
                files = glob.glob(os.path.join(out, "**", "*.txt"), recursive=True)
                files = [f for f in files if not f.endswith("export-log.txt") and " - attachments" not in f]
                ok = ok and len(files) == len(all_rows)
            elif fmt == "attachments":
                files = [f for f in glob.glob(os.path.join(out, "**", "*"), recursive=True) if os.path.isfile(f)]
                names = {os.path.basename(f) for f in files}
                ok = ok and {"picture.png", "big file.bin", "notes.txt", "Forwarded original.eml"} <= names
            idx = os.path.join(out, "index.csv")
            ok = ok and os.path.isfile(idx) and os.path.isfile(os.path.join(out, "index.json"))
            record(f"export {fmt} ({label})", ok, detail)
        # only-email filter and cancellation
        res = export(all_rows, ExportOptions(fmt="txt", out_dir=os.path.join(out_root, "mailonly"), only_email=True,
                                              include_attachments=False))
        record(f"e-mail-only filter ({label})", res.exported == len(all_rows) - 1, res.summary())
        import threading
        ev = threading.Event()
        ev.set()
        res = export(all_rows, ExportOptions(fmt="txt", out_dir=os.path.join(out_root, "cancel")), cancel=ev)
        record(f"cancel ({label})", res.cancelled and res.exported == 0, res.summary())
    finally:
        pst.close()


def _check_real(path: str, record, tmp: str):
    pst = PSTFile(path)
    try:
        rows = list(pst.all_message_rows())
        opened = 0
        att = 0
        errors = []
        for r in rows:
            try:
                m = r.open()
                m.best_text()
                for a in m.attachments():
                    if a.is_embedded_message:
                        a.embedded_message
                    else:
                        a.data
                    att += 1
                opened += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"0x{r.nid:x}: {exc}")
        record(f"real file {os.path.basename(path)}: open every message", not errors,
               f"{pst.description}; {len(rows)} messages, {opened} opened, {att} attachments" + (f"; first error {errors[0]}" if errors else ""))
        out = os.path.join(tmp, "real-export")
        res = export(rows, ExportOptions(fmt="eml", out_dir=out))
        record(f"real file {os.path.basename(path)}: EML export", res.exported == len(rows) and not res.errors, res.summary())
    finally:
        pst.close()
