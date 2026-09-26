"""Self-test: builds synthetic PST files, reads them back, exports every format.

    python mailex.py selftest            synthetic files only
    python mailex.py selftest FILE.pst   also open a real file and export it

Writes <app folder>/mailex-selftest.txt and returns 0 when everything
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

REPORT_NAME = "mailex-selftest.txt"

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
    tmp = tempfile.mkdtemp(prefix="mailex-selftest-")
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

        # ---- the other mailbox formats, built from the synthetic PST's own exports
        try:
            _check_sources(record, tmp, mailbox)
        except Exception as exc:  # noqa: BLE001
            record("other mailbox formats", False, f"{type(exc).__name__}: {exc}")
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


def _check_sources(record, tmp: str, mailbox_def):
    import shutil
    from .sources import (FilesSource, MailDirSource, MaildirSource, MboxSource, SingleFileSource, find_sources,
                          open_found, open_source)
    base = os.path.join(tmp, "sources")
    pst_path = os.path.join(base, "seed.pst")
    os.makedirs(base, exist_ok=True)
    synth.write_pst(pst_path, mailbox_def)
    pst = PSTFile(pst_path)
    seed_rows = list(pst.all_message_rows())
    n_all = len(seed_rows)
    inbox_msgs = {r.subject: r.open() for r in seed_rows if r.folder.name == "Inbox"}

    # -- EML tree -> FilesSource
    eml_dir = os.path.join(base, "eml")
    export(seed_rows, ExportOptions(fmt="eml", out_dir=eml_dir, write_index=False))
    src = FilesSource(eml_dir)
    folders = {f.name: f for f in src.root.walk()}
    rows = list(src.all_message_rows())
    uni = [r for r in rows if r.subject == UNICODE_SUBJECT]
    ok = len(rows) == n_all and "Inbox" in folders and "Bulk" in folders and len(folders["Bulk"].messages()) == 230 and bool(uni)
    detail = f"{len(rows)} messages, folders {sorted(folders)}"
    if uni:
        m = uni[0].open()
        atts = m.attachments()
        ok = ok and "<b>HTML</b>" in m.best_html() and len(atts) == 1 and atts[0].data.startswith(b"\x89PNG") \
            and atts[0].hidden and m.importance == 2 and len(m.recipients_of("Cc")) == 1 and m.raw_eml() is not None
    fw = [r for r in rows if r.subject.startswith("FW: with an embedded")]
    if fw:
        m = fw[0].open()
        emb = [a for a in m.attachments() if a.is_embedded_message]
        ok = ok and bool(emb) and emb[0].embedded_message is not None and emb[0].embedded_message.subject == "Forwarded original"
    record("EML folder source", ok, detail)
    # passthrough: exporting the EML source again gives identical bytes
    out2 = os.path.join(base, "eml-again")
    export(rows, ExportOptions(fmt="eml", out_dir=out2, write_index=False, per_file_subfolder=False))
    a = sorted(glob.glob(os.path.join(eml_dir, "**", "*.eml"), recursive=True))
    b = sorted(glob.glob(os.path.join(out2, "**", "*.eml"), recursive=True))
    same = len(a) == len(b) and all(open(x, "rb").read() == open(y, "rb").read() for x, y in zip(a, b))
    record("EML passthrough is byte-identical", same, f"{len(a)} vs {len(b)} files")

    # -- .emlx
    emlx_dir = os.path.join(base, "emlx")
    os.makedirs(emlx_dir)
    raw = open(a[0], "rb").read()
    with open(os.path.join(emlx_dir, "1.emlx"), "wb") as fh:
        fh.write(str(len(raw)).encode() + b"\n" + raw + b"<?xml version=\"1.0\"?><plist/>")
    src = SingleFileSource(os.path.join(emlx_dir, "1.emlx"))
    m = list(src.all_message_rows())[0].open()
    record("EMLX single file", m.raw_eml() == raw and bool(m.subject), m.subject[:40])

    # -- MBOX file, mail folder (Thunderbird layout), Apple package
    mbox_dir = os.path.join(base, "mbox")
    export(seed_rows, ExportOptions(fmt="mbox", out_dir=mbox_dir, write_index=False, per_file_subfolder=False))
    mboxes = sorted(glob.glob(os.path.join(mbox_dir, "**", "*.mbox"), recursive=True))
    bulk = [x for x in mboxes if os.path.basename(x) == "Bulk.mbox"][0]
    src = MboxSource(bulk)
    rows = list(src.all_message_rows())
    m = rows[5].open()
    record("MBOX file source", len(rows) == 230 and m.subject.startswith("Bulk message") and m.raw_eml() is not None
           and m.read, f"{len(rows)} messages in {os.path.basename(bulk)}")
    src.close()
    tb = os.path.join(base, "thunderbird", "Mail", "Local Folders")
    os.makedirs(os.path.join(tb, "Archive.sbd"))
    inbox_mbox = [x for x in mboxes if os.path.basename(x) == "Inbox.mbox"][0]
    shutil.copy(inbox_mbox, os.path.join(tb, "Inbox"))
    shutil.copy(bulk, os.path.join(tb, "Archive"))
    shutil.copy([x for x in mboxes if os.path.basename(x) == "Sent Items.mbox"][0], os.path.join(tb, "Archive.sbd", "2024"))
    with open(os.path.join(tb, "Inbox.msf"), "w") as fh:
        fh.write("// <!-- <mdb:mork:z v=\"1.4\"/> -->")
    src = MailDirSource(tb)
    folders = {f.path_str: len(f.messages()) for f in src.root.walk()}
    record("Thunderbird mail folder source", folders.get("Local Folders/Inbox") == 8 and folders.get("Local Folders/Archive") == 230
           and folders.get("Local Folders/Archive/2024") == 1 and not any(".msf" in k for k in folders), str(folders))
    src.close()
    apple = os.path.join(base, "Apple.mbox")
    os.makedirs(apple)
    shutil.copy(inbox_mbox, os.path.join(apple, "mbox"))
    src = open_source(apple)
    record("Apple Mail .mbox package", len(list(src.all_message_rows())) == 8 and src.name == "Apple.mbox", src.description)
    src.close()

    # -- Maildir with a Courier-style sub-folder and read/unread flags
    md = os.path.join(base, "Maildir")
    for sub in ("cur", "new", "tmp", ".Projects/cur", ".Projects/new", ".Projects/tmp"):
        os.makedirs(os.path.join(md, sub))
    # ':' is the Unix flag separator; on NTFS a colon makes an alternate data
    # stream instead of a file name, so Maildirs there use '!' - as does this test
    sep = "!" if os.name == "nt" else ":"
    for i, x in enumerate(a[:6]):
        shutil.copy(x, os.path.join(md, "cur" if i % 2 == 0 else "new", f"{i}.mail" + (sep + "2,S" if i % 2 == 0 else "")))
    for i, x in enumerate(a[6:9]):
        shutil.copy(x, os.path.join(md, ".Projects", "cur", f"{i}.mail{sep}2,"))
    src = MaildirSource(md)
    root_rows = src.root.messages()
    proj = [f for f in src.root.walk() if f.name == "Projects"]
    ok = len(root_rows) == 6 and sum(1 for r in root_rows if r.read) == 3 and proj and len(proj[0].messages()) == 3 \
        and not proj[0].messages()[0].read
    record("Maildir source", ok, f"{len(root_rows)} + {len(proj[0].messages()) if proj else 0} messages")

    # -- .msg files through the compound-file writer and reader
    msg_dir = os.path.join(base, "msg")
    os.makedirs(msg_dir)
    for i, m in enumerate(mailbox_def[0].subfolders[0].messages):
        synth.write_msg(os.path.join(msg_dir, f"{i}.msg"), m)
    src = FilesSource(msg_dir)
    rows = list(src.all_message_rows())
    got = {r.subject: r.open() for r in rows}
    ok = len(rows) == 8
    m = got.get(UNICODE_SUBJECT)
    ok = ok and m is not None and "<b>HTML</b>" in m.best_html() and m.attachments()[0].data.startswith(b"\x89PNG") \
        and m.attachments()[0].hidden and len(m.recipients()) == 3 and m.sender_email == "zoe@example.com"
    m = got.get("Long body and a big attachment")
    ok = ok and m is not None and m.body_text == LONG_BODY and len(m.attachments()[0].data) == 50_000
    m = got.get("RTF only body")
    ok = ok and m is not None and "Hello from RTF with café" in m.best_text()
    m = got.get("FW: with an embedded message")
    emb = m.attachments()[0].embedded_message if m and m.attachments() else None
    ok = ok and emb is not None and emb.subject == "Forwarded original" and emb.body_text == "This is the embedded message body."
    record("Outlook .msg files (compound file reader)", ok, f"{len(rows)} files")

    # -- the layouts real machines have
    live = os.path.join(base, "applelive", "Inbox.mbox", "0A1B2C3D-0000-4000-8000-000000000001", "Data", "1", "Messages")
    os.makedirs(live)
    for i, x in enumerate(a[:3]):
        raw2 = open(x, "rb").read()
        with open(os.path.join(live, f"{i + 1}.emlx"), "wb") as fh:
            fh.write(str(len(raw2)).encode() + b"\n" + raw2 + b"<plist/>")
    child = os.path.join(base, "applelive", "Inbox.mbox", "Receipts.mbox", "0A1B2C3D-0000-4000-8000-000000000002", "Data", "Messages")
    os.makedirs(child)
    raw2 = open(a[3], "rb").read()
    with open(os.path.join(child, "9.emlx"), "wb") as fh:
        fh.write(str(len(raw2)).encode() + b"\n" + raw2 + b"<plist/>")
    src = open_source(os.path.join(base, "applelive", "Inbox.mbox"))
    counts = {f.path_str: len(f.messages()) for f in src.root.walk()}
    record("Apple Mail live mailbox (V2+ layout, nested child)", counts == {"Inbox": 3, "Inbox/Receipts": 1}, str(counts))
    found = find_sources(os.path.join(base, "applelive"))
    record("Apple Mail live mailbox found from its parent", found == [("mbox", os.path.join(base, "applelive", "Inbox.mbox"))], str(found))

    tb2 = os.path.join(base, "tb-empty", "Local Folders")
    os.makedirs(os.path.join(tb2, "Archives.sbd"))
    open(os.path.join(tb2, "Archives"), "wb").close()                       # 0-byte placeholder
    shutil.copy(bulk, os.path.join(tb2, "Archives.sbd", "2019"))
    found = find_sources(os.path.join(base, "tb-empty"))
    ok = found == [("mboxdir", tb2)]
    if ok:
        src = MailDirSource(tb2)
        counts = {f.path_str: len(f.messages()) for f in src.root.walk()}
        ok = counts.get("Local Folders/Archives/2019") == 230 and counts.get("Local Folders/Archives") == 0
    record("Thunderbird folder whose own mbox is empty", ok, str(found))

    # a Maildir copied through Windows uses '!' (or ';') instead of ':' in the flags
    md2 = os.path.join(base, "Maildir-win")
    for sub in ("cur", "new", "tmp"):
        os.makedirs(os.path.join(md2, sub))
    shutil.copy(a[0], os.path.join(md2, "cur", "1.mail!2,S"))
    shutil.copy(a[1], os.path.join(md2, "cur", "2.mail;2,"))
    shutil.copy(a[2], os.path.join(md2, "cur", "3.mail!2,ST"))
    rows2 = MaildirSource(md2).root.messages()
    record("Maildir flags with '!' separator, trashed skipped", len(rows2) == 2 and rows2[0].read and not rows2[1].read,
           f"{len(rows2)} rows")

    # mbox read from several threads at once must never mix messages up
    import threading
    src = MboxSource(bulk)
    rows2 = list(src.all_message_rows())
    bad = []

    def hammer(offset):
        for i in range(offset, len(rows2), 7):
            m = rows2[i].open()
            if m.subject != rows2[i].subject:
                bad.append(i)
    ts = [threading.Thread(target=hammer, args=(k,)) for k in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    record("MBOX reads from four threads stay consistent", not bad, f"{len(bad)} mismatches")

    # odd MIME: unknown charset, calendar part, split body
    odd = (b"From: a@example.com\r\nTo: b@example.com\r\nSubject: odd\r\nDate: Tue, 05 Mar 2024 09:30:15 +0000\r\n"
           b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"B\"\r\n\r\n"
           b"--B\r\nContent-Type: text/plain; charset=x-user-defined\r\n\r\nfirst part\r\n"
           b"--B\r\nContent-Type: image/png\r\nContent-Disposition: inline\r\n\r\nPNG\r\n"
           b"--B\r\nContent-Type: text/plain; charset=\"iso-8859-8-i\"\r\n\r\nsecond part\r\n"
           b"--B\r\nContent-Type: text/calendar; method=REQUEST\r\n\r\nBEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n--B--\r\n")
    from .sources import MimeMessage
    mm = MimeMessage(src, odd)
    atts = mm.attachments()
    record("odd MIME: unknown charsets, split body, calendar part", mm.body_text == "first part\n\nsecond part"
           and [x.mime_type for x in atts] == ["image/png", "text/calendar"], f"{mm.body_text!r} {atts}")

    # a corrupted .msg must fail quickly and cleanly, not hang
    import random
    import time as _time
    raw_msg = open(os.path.join(msg_dir, "1.msg"), "rb").read()
    random.seed(7)
    worst = 0.0
    for _ in range(40):
        b = bytearray(raw_msg)
        for _j in range(random.randint(1, 30)):
            b[random.randrange(min(len(b), 4096))] = random.randrange(256)
        t0 = _time.time()
        try:
            from .cfb import CompoundFile
            from .msgfile import MsgMessage
            mm = MsgMessage(src, None, cf=CompoundFile(bytes(b)))
            mm.subject
            mm.best_text()
            [(x.filename, x.data) for x in mm.attachments()]
        except Exception:  # noqa: BLE001
            pass
        worst = max(worst, _time.time() - t0)
    record("corrupt .msg files fail fast", worst < 2.0, f"worst {worst:.2f}s")

    # -- find_sources over the lot, then one bulk export of everything
    found = find_sources(base)
    kinds = sorted(k for k, _p in found)
    ok = kinds == ["files", "maildir", "maildir", "mbox", "mbox", "mboxdir", "mboxdir", "mboxdir", "pst"]
    record("find_sources on a mixed folder", ok, str(kinds))
    srcs_all = [open_found(k, p) for k, p in found]
    ids = [(r.pst.path, r.folder.path_str, r.subject) for s_ in srcs_all for r in s_.all_message_rows()]
    record("nothing is listed twice", len(ids) == len(set(ids)), f"{len(ids)} rows, {len(set(ids))} distinct")
    for s_ in srcs_all:
        s_.close()
    srcs = [open_found(k, p) for k, p in found]
    rows = [r for s_ in srcs for r in s_.all_message_rows()]
    total = len(rows)
    out = os.path.join(base, "everything")
    for fmt in ("eml", "html"):
        res = export(rows, ExportOptions(fmt=fmt, out_dir=os.path.join(out, fmt), include_attachments=(fmt == "eml")))
        record(f"bulk export of every source kind ({fmt})", res.exported == total and not res.errors,
               res.summary() + f" from {len(srcs)} sources" + ("; " + res.errors[0] if res.errors else ""))
    for s_ in srcs:
        s_.close()
    pst.close()
