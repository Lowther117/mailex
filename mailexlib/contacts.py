"""Contact items: the MAPI contact properties read into one shape, then written
out as vCard 3.0 (RFC 2426), a CSV row, or readable text for the preview.

The fixed properties ([MS-OXOCNTC], all below 0x8000) hold the names, phones
and postal addresses. The e-mail addresses, "file as" name, instant messaging
address and the assembled work address are *named* properties in
PSETID_Address and need the file's name-to-id map - see namedprops.py.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from . import mapi
from . import namedprops as np
from .filters import is_contact_class

# fixed contact property ids
PR_GIVEN_NAME = 0x3A06
PR_INITIALS = 0x3A0A
PR_SURNAME = 0x3A11
PR_MIDDLE_NAME = 0x3A44
PR_DISPLAY_NAME_PREFIX = 0x3A45
PR_GENERATION = 0x3A05
PR_NICKNAME = 0x3A4F
PR_COMPANY_NAME = 0x3A16
PR_TITLE = 0x3A17
PR_DEPARTMENT_NAME = 0x3A18
PR_OFFICE_LOCATION = 0x3A19
PR_PROFESSION = 0x3A46
PR_MANAGER_NAME = 0x3A4E
PR_ASSISTANT = 0x3A30
PR_SPOUSE_NAME = 0x3A48
PR_BUSINESS_TELEPHONE_NUMBER = 0x3A08
PR_BUSINESS2_TELEPHONE_NUMBER = 0x3A1B
PR_HOME_TELEPHONE_NUMBER = 0x3A09
PR_HOME2_TELEPHONE_NUMBER = 0x3A2F
PR_MOBILE_TELEPHONE_NUMBER = 0x3A1C
PR_PRIMARY_TELEPHONE_NUMBER = 0x3A1A
PR_OTHER_TELEPHONE_NUMBER = 0x3A1F
PR_PAGER_TELEPHONE_NUMBER = 0x3A21
PR_ASSISTANT_TELEPHONE_NUMBER = 0x3A2E
PR_COMPANY_MAIN_PHONE_NUMBER = 0x3A57
PR_BUSINESS_FAX_NUMBER = 0x3A24
PR_HOME_FAX_NUMBER = 0x3A25
PR_POSTAL_ADDRESS = 0x3A15
PR_BUSINESS_ADDRESS_COUNTRY = 0x3A26
PR_BUSINESS_ADDRESS_CITY = 0x3A27
PR_BUSINESS_ADDRESS_STATE = 0x3A28
PR_BUSINESS_ADDRESS_STREET = 0x3A29
PR_BUSINESS_ADDRESS_POSTAL_CODE = 0x3A2A
PR_BUSINESS_ADDRESS_PO_BOX = 0x3A2B
PR_HOME_ADDRESS_CITY = 0x3A59
PR_HOME_ADDRESS_COUNTRY = 0x3A5A
PR_HOME_ADDRESS_POSTAL_CODE = 0x3A5B
PR_HOME_ADDRESS_STATE = 0x3A5C
PR_HOME_ADDRESS_STREET = 0x3A5D
PR_HOME_ADDRESS_PO_BOX = 0x3A5E
PR_OTHER_ADDRESS_CITY = 0x3A5F
PR_OTHER_ADDRESS_COUNTRY = 0x3A60
PR_OTHER_ADDRESS_POSTAL_CODE = 0x3A61
PR_OTHER_ADDRESS_STATE = 0x3A62
PR_OTHER_ADDRESS_STREET = 0x3A63
PR_OTHER_ADDRESS_PO_BOX = 0x3A64
PR_BIRTHDAY = 0x3A42
PR_WEDDING_ANNIVERSARY = 0x3A41
PR_BUSINESS_HOME_PAGE = 0x3A51
PR_PERSONAL_HOME_PAGE = 0x3A50

CONTACT_TAG_NAMES = {
    PR_GIVEN_NAME: "GivenName", PR_INITIALS: "Initials", PR_SURNAME: "Surname", PR_MIDDLE_NAME: "MiddleName",
    PR_DISPLAY_NAME_PREFIX: "DisplayNamePrefix", PR_GENERATION: "Generation", PR_NICKNAME: "Nickname",
    PR_COMPANY_NAME: "CompanyName", PR_TITLE: "Title", PR_DEPARTMENT_NAME: "DepartmentName",
    PR_OFFICE_LOCATION: "OfficeLocation", PR_PROFESSION: "Profession", PR_MANAGER_NAME: "ManagerName",
    PR_ASSISTANT: "Assistant", PR_SPOUSE_NAME: "SpouseName", PR_BUSINESS_TELEPHONE_NUMBER: "BusinessTelephoneNumber",
    PR_BUSINESS2_TELEPHONE_NUMBER: "Business2TelephoneNumber", PR_HOME_TELEPHONE_NUMBER: "HomeTelephoneNumber",
    PR_HOME2_TELEPHONE_NUMBER: "Home2TelephoneNumber", PR_MOBILE_TELEPHONE_NUMBER: "MobileTelephoneNumber",
    PR_PRIMARY_TELEPHONE_NUMBER: "PrimaryTelephoneNumber", PR_OTHER_TELEPHONE_NUMBER: "OtherTelephoneNumber",
    PR_PAGER_TELEPHONE_NUMBER: "PagerTelephoneNumber", PR_ASSISTANT_TELEPHONE_NUMBER: "AssistantTelephoneNumber",
    PR_COMPANY_MAIN_PHONE_NUMBER: "CompanyMainTelephoneNumber", PR_BUSINESS_FAX_NUMBER: "BusinessFaxNumber",
    PR_HOME_FAX_NUMBER: "HomeFaxNumber", PR_POSTAL_ADDRESS: "PostalAddress",
    PR_BUSINESS_ADDRESS_COUNTRY: "Country", PR_BUSINESS_ADDRESS_CITY: "Locality", PR_BUSINESS_ADDRESS_STATE: "StateOrProvince",
    PR_BUSINESS_ADDRESS_STREET: "StreetAddress", PR_BUSINESS_ADDRESS_POSTAL_CODE: "PostalCode",
    PR_BUSINESS_ADDRESS_PO_BOX: "PostOfficeBox", PR_HOME_ADDRESS_CITY: "HomeAddressCity",
    PR_HOME_ADDRESS_COUNTRY: "HomeAddressCountry", PR_HOME_ADDRESS_POSTAL_CODE: "HomeAddressPostalCode",
    PR_HOME_ADDRESS_STATE: "HomeAddressStateOrProvince", PR_HOME_ADDRESS_STREET: "HomeAddressStreet",
    PR_HOME_ADDRESS_PO_BOX: "HomeAddressPostOfficeBox", PR_OTHER_ADDRESS_CITY: "OtherAddressCity",
    PR_OTHER_ADDRESS_COUNTRY: "OtherAddressCountry", PR_OTHER_ADDRESS_POSTAL_CODE: "OtherAddressPostalCode",
    PR_OTHER_ADDRESS_STATE: "OtherAddressStateOrProvince", PR_OTHER_ADDRESS_STREET: "OtherAddressStreet",
    PR_OTHER_ADDRESS_PO_BOX: "OtherAddressPostOfficeBox", PR_BIRTHDAY: "Birthday",
    PR_WEDDING_ANNIVERSARY: "WeddingAnniversary", PR_BUSINESS_HOME_PAGE: "BusinessHomePage",
    PR_PERSONAL_HOME_PAGE: "PersonalHomePage",
}
mapi.TAG_NAMES.update({k: v for k, v in CONTACT_TAG_NAMES.items() if k not in mapi.TAG_NAMES})


@dataclass
class Address:
    street: str = ""
    city: str = ""
    state: str = ""
    postcode: str = ""
    country: str = ""
    po_box: str = ""

    def __bool__(self) -> bool:
        return any((self.street, self.city, self.state, self.postcode, self.country, self.po_box))

    def one_line(self) -> str:
        parts = [self.po_box, self.street.replace("\n", ", "), self.city, self.state, self.postcode, self.country]
        return ", ".join(p for p in parts if p)


@dataclass
class Contact:
    display_name: str = ""
    file_under: str = ""
    prefix: str = ""
    given: str = ""
    middle: str = ""
    surname: str = ""
    suffix: str = ""
    nickname: str = ""
    company: str = ""
    title: str = ""
    department: str = ""
    office: str = ""
    profession: str = ""
    manager: str = ""
    assistant: str = ""
    spouse: str = ""
    emails: List[Tuple[str, str]] = field(default_factory=list)     # (display name, address), Email1..3 in order
    im_address: str = ""
    phones: List[Tuple[str, str]] = field(default_factory=list)     # (kind, number): work, home, cell, fax...
    business: Address = field(default_factory=Address)
    home: Address = field(default_factory=Address)
    other: Address = field(default_factory=Address)
    postal_address: str = ""             # Outlook's "mailing address" as one string
    birthday: Optional[_dt.datetime] = None
    anniversary: Optional[_dt.datetime] = None
    urls: List[Tuple[str, str]] = field(default_factory=list)       # (kind, url)
    categories: List[str] = field(default_factory=list)
    notes: str = ""
    warnings: List[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        parts = [self.prefix, self.given, self.middle, self.surname, self.suffix]
        return " ".join(p for p in parts if p)

    @property
    def name(self) -> str:
        """Best display name for file names and headings."""
        return self.display_name or self.full_name or self.file_under or self.company or (self.emails[0][1] if self.emails else "") or "contact"

    @property
    def email(self) -> str:
        return self.emails[0][1] if self.emails else ""


_PHONES = [("work", PR_BUSINESS_TELEPHONE_NUMBER), ("work", PR_BUSINESS2_TELEPHONE_NUMBER),
           ("home", PR_HOME_TELEPHONE_NUMBER), ("home", PR_HOME2_TELEPHONE_NUMBER),
           ("cell", PR_MOBILE_TELEPHONE_NUMBER), ("pref", PR_PRIMARY_TELEPHONE_NUMBER),
           ("voice", PR_OTHER_TELEPHONE_NUMBER), ("pager", PR_PAGER_TELEPHONE_NUMBER),
           ("work,fax", PR_BUSINESS_FAX_NUMBER), ("home,fax", PR_HOME_FAX_NUMBER),
           ("work,voice", PR_COMPANY_MAIN_PHONE_NUMBER), ("assistant", PR_ASSISTANT_TELEPHONE_NUMBER)]
_EMAILS = [(np.LID_EMAIL1_DISPLAY_NAME, np.LID_EMAIL1_EMAIL_ADDRESS, np.LID_EMAIL1_ORIGINAL_DISPLAY_NAME, np.LID_EMAIL1_ADDR_TYPE),
           (np.LID_EMAIL2_DISPLAY_NAME, np.LID_EMAIL2_EMAIL_ADDRESS, np.LID_EMAIL2_ORIGINAL_DISPLAY_NAME, np.LID_EMAIL2_ADDR_TYPE),
           (np.LID_EMAIL3_DISPLAY_NAME, np.LID_EMAIL3_EMAIL_ADDRESS, np.LID_EMAIL3_ORIGINAL_DISPLAY_NAME, np.LID_EMAIL3_ADDR_TYPE)]


def is_contact(msg) -> bool:
    return is_contact_class(getattr(msg, "message_class", "")) and hasattr(msg, "named")


def parse_contact(msg) -> Contact:
    """Read a contact item (PST Message or MsgMessage) into a Contact."""
    t = msg.text
    c = Contact()
    c.display_name = t(mapi.PR_DISPLAY_NAME).strip() or msg.subject
    c.prefix = t(PR_DISPLAY_NAME_PREFIX).strip()
    c.given = t(PR_GIVEN_NAME).strip()
    c.middle = t(PR_MIDDLE_NAME).strip()
    c.surname = t(PR_SURNAME).strip()
    c.suffix = t(PR_GENERATION).strip()
    c.nickname = t(PR_NICKNAME).strip()
    c.company = t(PR_COMPANY_NAME).strip()
    c.title = t(PR_TITLE).strip()
    c.department = t(PR_DEPARTMENT_NAME).strip()
    c.office = t(PR_OFFICE_LOCATION).strip()
    c.profession = t(PR_PROFESSION).strip()
    c.manager = t(PR_MANAGER_NAME).strip()
    c.assistant = t(PR_ASSISTANT).strip()
    c.spouse = t(PR_SPOUSE_NAME).strip()
    for kind, pid in _PHONES:
        v = t(pid).strip()
        if v:
            c.phones.append((kind, v))
    c.business = Address(t(PR_BUSINESS_ADDRESS_STREET), t(PR_BUSINESS_ADDRESS_CITY), t(PR_BUSINESS_ADDRESS_STATE),
                         t(PR_BUSINESS_ADDRESS_POSTAL_CODE), t(PR_BUSINESS_ADDRESS_COUNTRY), t(PR_BUSINESS_ADDRESS_PO_BOX))
    c.home = Address(t(PR_HOME_ADDRESS_STREET), t(PR_HOME_ADDRESS_CITY), t(PR_HOME_ADDRESS_STATE),
                     t(PR_HOME_ADDRESS_POSTAL_CODE), t(PR_HOME_ADDRESS_COUNTRY), t(PR_HOME_ADDRESS_PO_BOX))
    c.other = Address(t(PR_OTHER_ADDRESS_STREET), t(PR_OTHER_ADDRESS_CITY), t(PR_OTHER_ADDRESS_STATE),
                      t(PR_OTHER_ADDRESS_POSTAL_CODE), t(PR_OTHER_ADDRESS_COUNTRY), t(PR_OTHER_ADDRESS_PO_BOX))
    c.postal_address = t(PR_POSTAL_ADDRESS).strip()
    bd = msg.get(PR_BIRTHDAY)
    c.birthday = bd if isinstance(bd, _dt.datetime) else None
    an = msg.get(PR_WEDDING_ANNIVERSARY)
    c.anniversary = an if isinstance(an, _dt.datetime) else None
    for kind, pid in (("work", PR_BUSINESS_HOME_PAGE), ("home", PR_PERSONAL_HOME_PAGE)):
        v = t(pid).strip()
        if v:
            c.urls.append((kind, v))
    c.notes = msg.best_text().strip()

    # the named part
    nm = msg.name_map()
    A = np.PSETID_ADDRESS
    if nm.ok or len(nm):
        c.file_under = msg.named_text(A, np.LID_FILE_UNDER).strip()
        for disp_id, addr_id, orig_id, type_id in _EMAILS:
            addr = msg.named_text(A, addr_id).strip()
            disp = msg.named_text(A, disp_id).strip()
            orig = msg.named_text(A, orig_id).strip()
            atype = msg.named_text(A, type_id).strip().upper()
            if atype == "EX" and "@" in orig:
                addr = orig                     # Exchange DN in the address slot; the SMTP form is kept beside it
            if not addr and "@" in orig:
                addr = orig
            if addr:
                if disp == addr or disp.endswith(f"({addr})"):
                    disp = ""
                c.emails.append((disp, addr))
        c.im_address = msg.named_text(A, np.LID_IM_ADDRESS).strip()
        if not c.business:
            # Outlook 2003+ keeps the work address in named parts as well
            c.business = Address(msg.named_text(A, np.LID_WORK_ADDRESS_STREET), msg.named_text(A, np.LID_WORK_ADDRESS_CITY),
                                 msg.named_text(A, np.LID_WORK_ADDRESS_STATE), msg.named_text(A, np.LID_WORK_ADDRESS_POSTAL_CODE),
                                 msg.named_text(A, np.LID_WORK_ADDRESS_COUNTRY), "")
        if not c.postal_address:
            c.postal_address = msg.named_text(A, np.LID_WORK_ADDRESS).strip() or msg.named_text(A, np.LID_HOME_ADDRESS).strip()
        cats = msg.named(np.PS_PUBLIC_STRINGS, "Keywords")
        if isinstance(cats, list):
            c.categories = [str(x) for x in cats if x]
        elif isinstance(cats, str) and cats:
            c.categories = [cats]
    if nm.error:
        c.warnings.append(f"{nm.error}; the contact's e-mail addresses could not be read")
    return c


# -- vCard 3.0 ----------------------------------------------------------------
def vcard_escape(s: str) -> str:
    """Text values: backslash, comma and semicolon are escaped, newlines become \\n."""
    return (s or "").replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "").replace(",", "\\,").replace(";", "\\;")


def fold_line(line: str, limit: int = 75) -> str:
    """RFC 2426 folding: no line longer than 75 octets, continuation lines start
    with a space, and a multi-byte character is never split."""
    data = line.encode("utf-8")
    if len(data) <= limit:
        return line
    out = []
    cur = bytearray()
    budget = limit
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > budget:
            out.append(cur.decode("utf-8"))
            cur = bytearray(b" ")
            budget = limit
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n".join(out)


def _date(d: Optional[_dt.datetime]) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


def vcard(c: Contact) -> str:
    """One card, CRLF line endings, folded and escaped."""
    lines = ["BEGIN:VCARD", "VERSION:3.0", "PRODID:-//Mailex//EN"]
    lines.append("FN:" + vcard_escape(c.name))
    n = ";".join(vcard_escape(x) for x in (c.surname, c.given, c.middle, c.prefix, c.suffix))
    lines.append("N:" + n)
    if c.nickname:
        lines.append("NICKNAME:" + vcard_escape(c.nickname))
    if c.company or c.department:
        lines.append("ORG:" + vcard_escape(c.company) + (";" + vcard_escape(c.department) if c.department else ""))
    if c.title:
        lines.append("TITLE:" + vcard_escape(c.title))
    if c.profession:
        lines.append("ROLE:" + vcard_escape(c.profession))
    for i, (kind, number) in enumerate(c.phones):
        types = kind.upper()
        if i == 0:
            types += ",PREF" if "PREF" not in types else ""
        lines.append(f"TEL;TYPE={types}:" + vcard_escape(number))
    for i, (_disp, addr) in enumerate(c.emails):
        lines.append(f"EMAIL;TYPE=INTERNET{',PREF' if i == 0 else ''}:" + vcard_escape(addr))
    if c.im_address:
        lines.append("IMPP:" + vcard_escape(c.im_address))
    for kind, adr in (("WORK", c.business), ("HOME", c.home), ("POSTAL", c.other)):
        if adr:
            parts = (adr.po_box, "", adr.street, adr.city, adr.state, adr.postcode, adr.country)
            lines.append(f"ADR;TYPE={kind}:" + ";".join(vcard_escape(p) for p in parts))
            if kind in ("WORK", "HOME"):
                label = "\n".join(p for p in (adr.street, adr.city, adr.state, adr.postcode, adr.country) if p)
                lines.append(f"LABEL;TYPE={kind}:" + vcard_escape(label))
    if c.birthday:
        lines.append("BDAY:" + _date(c.birthday))
    if c.anniversary:
        lines.append("X-ANNIVERSARY:" + _date(c.anniversary))
    for kind, url in c.urls:
        lines.append(f"URL;TYPE={kind.upper()}:" + vcard_escape(url))
    if c.spouse:
        lines.append("X-SPOUSE:" + vcard_escape(c.spouse))
    if c.manager:
        lines.append("X-MANAGER:" + vcard_escape(c.manager))
    if c.assistant:
        lines.append("X-ASSISTANT:" + vcard_escape(c.assistant))
    if c.categories:
        lines.append("CATEGORIES:" + ",".join(vcard_escape(x) for x in c.categories))
    if c.notes:
        lines.append("NOTE:" + vcard_escape(c.notes))
    lines.append("REV:" + _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    lines.append("END:VCARD")
    return "\r\n".join(fold_line(x) for x in lines) + "\r\n"


# -- CSV --------------------------------------------------------------------------
CSV_COLUMNS = ["file", "folder", "display_name", "file_under", "prefix", "first_name", "middle_name", "last_name", "suffix",
               "nickname", "company", "job_title", "department", "office", "profession", "manager", "assistant", "spouse",
               "email1", "email1_display", "email2", "email2_display", "email3", "email3_display", "im_address",
               "business_phone", "business_phone2", "home_phone", "home_phone2", "mobile_phone", "primary_phone",
               "other_phone", "pager", "business_fax", "home_fax", "company_phone", "assistant_phone",
               "business_street", "business_city", "business_state", "business_postcode", "business_country", "business_po_box",
               "home_street", "home_city", "home_state", "home_postcode", "home_country", "home_po_box",
               "other_street", "other_city", "other_state", "other_postcode", "other_country", "other_po_box",
               "mailing_address", "birthday", "anniversary", "web_page", "personal_web_page", "categories", "notes",
               "exported_as"]

_PHONE_COLS = {("work", 0): "business_phone", ("work", 1): "business_phone2", ("home", 0): "home_phone",
               ("home", 1): "home_phone2", ("cell", 0): "mobile_phone", ("pref", 0): "primary_phone",
               ("voice", 0): "other_phone", ("pager", 0): "pager", ("work,fax", 0): "business_fax",
               ("home,fax", 0): "home_fax", ("work,voice", 0): "company_phone", ("assistant", 0): "assistant_phone"}


def csv_row(c: Contact, file: str = "", folder: str = "", exported_as: str = "") -> Dict[str, str]:
    r = {k: "" for k in CSV_COLUMNS}
    r.update(file=file, folder=folder, display_name=c.name, file_under=c.file_under, prefix=c.prefix,
             first_name=c.given, middle_name=c.middle, last_name=c.surname, suffix=c.suffix, nickname=c.nickname,
             company=c.company, job_title=c.title, department=c.department, office=c.office, profession=c.profession,
             manager=c.manager, assistant=c.assistant, spouse=c.spouse, im_address=c.im_address,
             mailing_address=c.postal_address.replace("\r\n", " / ").replace("\n", " / "),
             birthday=_date(c.birthday), anniversary=_date(c.anniversary), categories="; ".join(c.categories),
             notes=c.notes, exported_as=exported_as)
    for i, (disp, addr) in enumerate(c.emails[:3]):
        r[f"email{i + 1}"] = addr
        r[f"email{i + 1}_display"] = disp
    seen: Dict[str, int] = {}
    for kind, number in c.phones:
        n = seen.get(kind, 0)
        seen[kind] = n + 1
        col = _PHONE_COLS.get((kind, n))
        if col:
            r[col] = number
    for prefix, adr in (("business", c.business), ("home", c.home), ("other", c.other)):
        r[f"{prefix}_street"] = adr.street.replace("\n", " / ")
        r[f"{prefix}_city"] = adr.city
        r[f"{prefix}_state"] = adr.state
        r[f"{prefix}_postcode"] = adr.postcode
        r[f"{prefix}_country"] = adr.country
        r[f"{prefix}_po_box"] = adr.po_box
    for kind, url in c.urls:
        r["web_page" if kind == "work" else "personal_web_page"] = url
    return r


# -- readable text (preview, plain-text export) --------------------------------
_PHONE_LABELS = {"work": "Business", "home": "Home", "cell": "Mobile", "pref": "Primary", "voice": "Other",
                 "pager": "Pager", "work,fax": "Business fax", "home,fax": "Home fax", "work,voice": "Company",
                 "assistant": "Assistant"}


def contact_fields(c: Contact) -> List[Tuple[str, str]]:
    """(label, value) pairs in a sensible reading order, empty ones left out."""
    out: List[Tuple[str, str]] = [("Name", c.full_name or c.name)]
    if c.file_under and c.file_under != c.name:
        out.append(("File as", c.file_under))
    if c.nickname:
        out.append(("Nickname", c.nickname))
    if c.company:
        out.append(("Company", c.company))
    if c.title:
        out.append(("Job title", c.title))
    if c.department:
        out.append(("Department", c.department))
    if c.office:
        out.append(("Office", c.office))
    if c.profession:
        out.append(("Profession", c.profession))
    for i, (disp, addr) in enumerate(c.emails):
        out.append((f"E-mail {i + 1}" if len(c.emails) > 1 else "E-mail", f"{disp} <{addr}>" if disp else addr))
    if c.im_address:
        out.append(("IM address", c.im_address))
    for kind, number in c.phones:
        out.append((_PHONE_LABELS.get(kind, kind.title()), number))
    if c.business:
        out.append(("Business address", c.business.one_line()))
    if c.home:
        out.append(("Home address", c.home.one_line()))
    if c.other:
        out.append(("Other address", c.other.one_line()))
    if c.postal_address and c.postal_address.replace("\r\n", ", ").replace("\n", ", ") not in (c.business.one_line(), c.home.one_line()):
        out.append(("Mailing address", c.postal_address.replace("\r\n", ", ").replace("\n", ", ")))
    if c.birthday:
        out.append(("Birthday", _date(c.birthday)))
    if c.anniversary:
        out.append(("Anniversary", _date(c.anniversary)))
    for kind, url in c.urls:
        out.append(("Web page" if kind == "work" else "Personal web page", url))
    if c.manager:
        out.append(("Manager", c.manager))
    if c.assistant:
        out.append(("Assistant", c.assistant))
    if c.spouse:
        out.append(("Spouse", c.spouse))
    if c.categories:
        out.append(("Categories", ", ".join(c.categories)))
    return out


def contact_text(c: Contact) -> str:
    width = max((len(k) for k, _v in contact_fields(c)), default=10)
    lines = [f"{k.ljust(width)}  {v}" for k, v in contact_fields(c)]
    if c.notes:
        lines += ["", "Notes", c.notes]
    if c.warnings:
        lines += [""] + ["Note: " + w for w in c.warnings]
    return "\n".join(lines) + "\n"


def combined_vcf(cards: Iterable[str]) -> str:
    return "".join(cards)
