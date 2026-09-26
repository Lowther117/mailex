"""MAPI property tags, types and code pages used by the reader and exporters."""
from __future__ import annotations

import codecs
import datetime as _dt
import struct
import uuid
from typing import Any, Optional

# property types
PT_UNSPECIFIED = 0x0000
PT_NULL = 0x0001
PT_I2 = 0x0002
PT_LONG = 0x0003
PT_R4 = 0x0004
PT_DOUBLE = 0x0005
PT_CURRENCY = 0x0006
PT_APPTIME = 0x0007
PT_ERROR = 0x000A
PT_BOOLEAN = 0x000B
PT_OBJECT = 0x000D
PT_I8 = 0x0014
PT_STRING8 = 0x001E
PT_UNICODE = 0x001F
PT_SYSTIME = 0x0040
PT_CLSID = 0x0048
PT_SVREID = 0x00FB
PT_BINARY = 0x0102
PT_MV_FLAG = 0x1000

# fixed sizes of the scalar types (None = variable)
FIXED_SIZES = {
    PT_I2: 2, PT_LONG: 4, PT_R4: 4, PT_DOUBLE: 8, PT_CURRENCY: 8, PT_APPTIME: 8,
    PT_ERROR: 4, PT_BOOLEAN: 1, PT_I8: 8, PT_SYSTIME: 8, PT_CLSID: 16,
}
# types whose value fits inline in the 4-byte slot of a property context
PC_INLINE_TYPES = {PT_I2, PT_LONG, PT_R4, PT_ERROR, PT_BOOLEAN, PT_NULL, PT_UNSPECIFIED}

# property identifiers we care about
PR_IMPORTANCE = 0x0017
PR_MESSAGE_CLASS = 0x001A
PR_PRIORITY = 0x0026
PR_SENSITIVITY = 0x0036
PR_SUBJECT = 0x0037
PR_CLIENT_SUBMIT_TIME = 0x0039
PR_SENT_REPRESENTING_NAME = 0x0042
PR_SENT_REPRESENTING_EMAIL = 0x0065
PR_SENT_REPRESENTING_ADDRTYPE = 0x0064
PR_SUBJECT_PREFIX = 0x003D
PR_CONVERSATION_TOPIC = 0x0070
PR_TRANSPORT_HEADERS = 0x007D
PR_IN_REPLY_TO_ID = 0x1042
PR_INTERNET_REFERENCES = 0x1039
PR_SENDER_NAME = 0x0C1A
PR_SENDER_EMAIL = 0x0C1F
PR_SENDER_ADDRTYPE = 0x0C1E
PR_SENDER_SMTP = 0x5D01
PR_SENT_REPRESENTING_SMTP = 0x5D02
PR_DISPLAY_BCC = 0x0E02
PR_DISPLAY_CC = 0x0E03
PR_DISPLAY_TO = 0x0E04
PR_MESSAGE_DELIVERY_TIME = 0x0E06
PR_MESSAGE_FLAGS = 0x0E07
PR_MESSAGE_SIZE = 0x0E08
PR_HASATTACH = 0x0E1B
PR_NORMALIZED_SUBJECT = 0x0E1D
PR_BODY = 0x1000
PR_RTF_COMPRESSED = 0x1009
PR_HTML = 0x1013
PR_INTERNET_MESSAGE_ID = 0x1035
PR_LAST_MODIFICATION_TIME = 0x3008
PR_CREATION_TIME = 0x3007
PR_DISPLAY_NAME = 0x3001
PR_ADDRTYPE = 0x3002
PR_EMAIL_ADDRESS = 0x3003
PR_SMTP_ADDRESS = 0x39FE
PR_RECIPIENT_TYPE = 0x0C15
PR_RECIPIENT_DISPLAY_NAME = 0x5FF6
PR_CONTENT_COUNT = 0x3602
PR_CONTENT_UNREAD = 0x3603
PR_SUBFOLDERS = 0x360A
PR_CONTAINER_CLASS = 0x3613
PR_ATTACH_DATA = 0x3701
PR_ATTACH_EXTENSION = 0x3703
PR_ATTACH_FILENAME = 0x3704
PR_ATTACH_METHOD = 0x3705
PR_ATTACH_LONG_FILENAME = 0x3707
PR_ATTACH_MIME_TAG = 0x370E
PR_ATTACH_CONTENT_ID = 0x3712
PR_ATTACH_CONTENT_LOCATION = 0x3713
PR_ATTACH_FLAGS = 0x3714
PR_ATTACH_SIZE = 0x0E20
PR_ATTACHMENT_HIDDEN = 0x7FFE
PR_RENDERING_POSITION = 0x370B
PR_MESSAGE_CODEPAGE = 0x3FFD
PR_INTERNET_CPID = 0x3FDE
PR_LTP_ROW_ID = 0x67F2
PR_LTP_ROW_VER = 0x67F3
PR_IPM_SUBTREE_ENTRYID = 0x35E0
PR_IPM_WASTEBASKET_ENTRYID = 0x35E3
PR_IPM_SENTMAIL_ENTRYID = 0x35E4
PR_IPM_OUTBOX_ENTRYID = 0x35E2
PR_RECEIVED_BY_NAME = 0x0040
PR_RECEIVED_BY_EMAIL = 0x0076
PR_REPLY_RECIPIENT_NAMES = 0x0050
PR_READ_RECEIPT_REQUESTED = 0x0029
PR_ORIGINAL_DELIVERY_TIME = 0x0055
PR_LAST_VERB_EXECUTED = 0x1081

ATTACH_BY_VALUE = 1
ATTACH_BY_REFERENCE = 2
ATTACH_BY_REF_RESOLVE = 3
ATTACH_BY_REF_ONLY = 4
ATTACH_EMBEDDED_MSG = 5
ATTACH_OLE = 6

MSGFLAG_READ = 0x1
MSGFLAG_UNSENT = 0x8
MSGFLAG_HASATTACH = 0x10

TAG_NAMES = {
    PR_IMPORTANCE: "Importance", PR_MESSAGE_CLASS: "MessageClass", PR_PRIORITY: "Priority",
    PR_SENSITIVITY: "Sensitivity", PR_SUBJECT: "Subject", PR_CLIENT_SUBMIT_TIME: "ClientSubmitTime",
    PR_SENT_REPRESENTING_NAME: "SentRepresentingName", PR_SENT_REPRESENTING_EMAIL: "SentRepresentingEmailAddress",
    PR_SENT_REPRESENTING_ADDRTYPE: "SentRepresentingAddressType", PR_SUBJECT_PREFIX: "SubjectPrefix",
    PR_CONVERSATION_TOPIC: "ConversationTopic", PR_TRANSPORT_HEADERS: "TransportMessageHeaders",
    PR_IN_REPLY_TO_ID: "InReplyToId", PR_INTERNET_REFERENCES: "InternetReferences",
    PR_SENDER_NAME: "SenderName", PR_SENDER_EMAIL: "SenderEmailAddress", PR_SENDER_ADDRTYPE: "SenderAddressType",
    PR_SENDER_SMTP: "SenderSmtpAddress", PR_SENT_REPRESENTING_SMTP: "SentRepresentingSmtpAddress",
    PR_DISPLAY_BCC: "DisplayBcc", PR_DISPLAY_CC: "DisplayCc", PR_DISPLAY_TO: "DisplayTo",
    PR_MESSAGE_DELIVERY_TIME: "MessageDeliveryTime", PR_MESSAGE_FLAGS: "MessageFlags",
    PR_MESSAGE_SIZE: "MessageSize", PR_HASATTACH: "HasAttachments", PR_NORMALIZED_SUBJECT: "NormalizedSubject",
    PR_BODY: "Body", PR_RTF_COMPRESSED: "RtfCompressed", PR_HTML: "Html", PR_INTERNET_MESSAGE_ID: "InternetMessageId",
    PR_LAST_MODIFICATION_TIME: "LastModificationTime", PR_CREATION_TIME: "CreationTime",
    PR_DISPLAY_NAME: "DisplayName", PR_ADDRTYPE: "AddressType", PR_EMAIL_ADDRESS: "EmailAddress",
    PR_SMTP_ADDRESS: "SmtpAddress", PR_RECIPIENT_TYPE: "RecipientType",
    PR_RECIPIENT_DISPLAY_NAME: "RecipientDisplayName", PR_CONTENT_COUNT: "ContentCount",
    PR_CONTENT_UNREAD: "ContentUnreadCount", PR_SUBFOLDERS: "Subfolders", PR_CONTAINER_CLASS: "ContainerClass",
    PR_ATTACH_DATA: "AttachDataBinary", PR_ATTACH_EXTENSION: "AttachExtension",
    PR_ATTACH_FILENAME: "AttachFilename", PR_ATTACH_METHOD: "AttachMethod",
    PR_ATTACH_LONG_FILENAME: "AttachLongFilename", PR_ATTACH_MIME_TAG: "AttachMimeTag",
    PR_ATTACH_CONTENT_ID: "AttachContentId", PR_ATTACH_CONTENT_LOCATION: "AttachContentLocation",
    PR_ATTACH_FLAGS: "AttachFlags", PR_ATTACH_SIZE: "AttachSize", PR_ATTACHMENT_HIDDEN: "AttachmentHidden",
    PR_RENDERING_POSITION: "RenderingPosition", PR_MESSAGE_CODEPAGE: "MessageCodepage",
    PR_INTERNET_CPID: "InternetCodepage", PR_LTP_ROW_ID: "LtpRowId", PR_LTP_ROW_VER: "LtpRowVer",
    PR_RECEIVED_BY_NAME: "ReceivedByName", PR_RECEIVED_BY_EMAIL: "ReceivedByEmailAddress",
    PR_READ_RECEIPT_REQUESTED: "ReadReceiptRequested", PR_LAST_VERB_EXECUTED: "LastVerbExecuted",
    0x0E17: "MessageStatus", 0x0E1F: "RtfInSync", 0x3007: "CreationTime", 0x3FDE: "InternetCodepage",
    0x0C19: "SenderEntryId", 0x0041: "SentRepresentingEntryId", 0x0E1E: "AttachSizeTotal",
    0x0063: "ResponseRequested", 0x0E28: "PrimarySendAccount", 0x0E29: "NextSendAcct",
    0x1080: "IconIndex", 0x1090: "FlagStatus", 0x3010: "SearchKey", 0x300B: "SearchKey",
    0x001F: "MessageClassLong", 0x0E30: "ReplyRequested", 0x3FF1: "MessageLocaleId",
    0x3FFA: "LastModifierName", 0x4076: "ContentFilterSCL", 0x0E4C: "ReplyTemplateId",
}


def tag_name(prop_id: int) -> str:
    n = TAG_NAMES.get(prop_id)
    return n if n else f"0x{prop_id:04X}"


# -- code pages ---------------------------------------------------------------
_CODEPAGES = {
    0: "cp1252", 1200: "utf-16-le", 1201: "utf-16-be", 12000: "utf-32-le", 12001: "utf-32-be",
    65000: "utf-7", 65001: "utf-8", 20127: "ascii", 28591: "latin-1", 28592: "iso8859-2",
    28593: "iso8859-3", 28594: "iso8859-4", 28595: "iso8859-5", 28596: "iso8859-6",
    28597: "iso8859-7", 28598: "iso8859-8", 28599: "iso8859-9", 28603: "iso8859-13",
    28605: "iso8859-15", 932: "cp932", 936: "gbk", 949: "cp949", 950: "cp950",
    874: "cp874", 1250: "cp1250", 1251: "cp1251", 1252: "cp1252", 1253: "cp1253",
    1254: "cp1254", 1255: "cp1255", 1256: "cp1256", 1257: "cp1257", 1258: "cp1258",
    20866: "koi8-r", 21866: "koi8-u", 10000: "mac-roman", 10001: "shift_jis", 10007: "mac-cyrillic",
    20932: "euc-jp", 51932: "euc-jp", 51936: "gb2312", 51949: "euc-kr", 50220: "iso2022-jp",
    50221: "iso2022-jp", 50222: "iso2022-jp", 54936: "gb18030", 437: "cp437", 850: "cp850",
    852: "cp852", 855: "cp855", 857: "cp857", 860: "cp860", 861: "cp861", 862: "cp862",
    863: "cp863", 865: "cp865", 866: "cp866", 869: "cp869", 38598: "iso8859-8",
}


def codec_for(cpid: Optional[int], default: str = "cp1252") -> str:
    if cpid is None:
        return default
    name = _CODEPAGES.get(int(cpid))
    if name is None:
        name = f"cp{int(cpid)}"
    try:
        codecs.lookup(name)
    except LookupError:
        return default
    return name


def charset_for(cpid: Optional[int]) -> Optional[str]:
    """MIME charset label for a code page, or None if unknown."""
    if cpid is None:
        return None
    name = codec_for(cpid, "")
    if not name:
        return None
    return {"cp1252": "windows-1252", "latin-1": "iso-8859-1", "utf-16-le": "utf-16le",
            "utf-8": "utf-8", "gbk": "gbk", "cp932": "shift_jis", "cp949": "euc-kr",
            "cp950": "big5", "ascii": "us-ascii"}.get(name, name.replace("cp", "windows-") if name.startswith("cp125") else name)


def decode_string8(raw: bytes, cpid: Optional[int]) -> str:
    codec = codec_for(cpid)
    try:
        return raw.decode(codec)
    except (UnicodeDecodeError, LookupError):
        pass
    for alt in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(alt)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


# -- time ---------------------------------------------------------------------
_EPOCH_DIFF = 116444736000000000  # 100ns ticks between 1601-01-01 and 1970-01-01


def filetime_to_datetime(ft: int) -> Optional[_dt.datetime]:
    if ft <= 0 or ft == 0x7FFFFFFFFFFFFFFF or ft >= 0x7FFF35F4F06C7FFF:
        return None
    secs, rem = divmod(ft - _EPOCH_DIFF, 10_000_000)
    try:
        return _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(seconds=secs, microseconds=rem // 10)
    except (OverflowError, ValueError):
        return None


def apptime_to_datetime(days: float) -> Optional[_dt.datetime]:
    try:
        return _dt.datetime(1899, 12, 30, tzinfo=_dt.timezone.utc) + _dt.timedelta(days=days)
    except (OverflowError, ValueError):
        return None


# -- value decoding -----------------------------------------------------------
def decode_scalar(ptype: int, raw: bytes, cpid: Optional[int]) -> Any:
    """Decode the raw bytes of one single-valued property."""
    try:
        if ptype == PT_I2:
            return struct.unpack_from("<h", raw)[0]
        if ptype == PT_LONG:
            return struct.unpack_from("<i", raw)[0]
        if ptype == PT_ERROR:
            return struct.unpack_from("<I", raw)[0]
        if ptype == PT_R4:
            return struct.unpack_from("<f", raw)[0]
        if ptype == PT_DOUBLE:
            return struct.unpack_from("<d", raw)[0]
        if ptype == PT_CURRENCY:
            return struct.unpack_from("<q", raw)[0] / 10000.0
        if ptype == PT_APPTIME:
            return apptime_to_datetime(struct.unpack_from("<d", raw)[0])
        if ptype == PT_BOOLEAN:
            return bool(raw[0]) if raw else False
        if ptype == PT_I8:
            return struct.unpack_from("<q", raw)[0]
        if ptype == PT_SYSTIME:
            return filetime_to_datetime(struct.unpack_from("<Q", raw)[0])
        if ptype == PT_CLSID:
            return uuid.UUID(bytes_le=bytes(raw[:16]))
        if ptype == PT_STRING8:
            return decode_string8(bytes(raw), cpid).rstrip("\x00")
        if ptype == PT_UNICODE:
            if len(raw) % 2:
                raw = raw[:-1]
            return bytes(raw).decode("utf-16-le", errors="replace").rstrip("\x00")
        if ptype == PT_BINARY or ptype == PT_SVREID:
            return bytes(raw)
        if ptype == PT_OBJECT:
            if len(raw) >= 8:
                nid, size = struct.unpack_from("<II", raw)
                return ObjectRef(nid, size)
            return None
        if ptype == PT_NULL or ptype == PT_UNSPECIFIED:
            return None
    except (struct.error, ValueError):
        return None
    return bytes(raw)


def decode_multi(ptype: int, raw: bytes, cpid: Optional[int]) -> list:
    base = ptype & ~PT_MV_FLAG
    if base in FIXED_SIZES:
        size = FIXED_SIZES[base]
        return [decode_scalar(base, raw[i:i + size], cpid) for i in range(0, len(raw) - size + 1, size)]
    # variable-size: count, offsets, data
    if len(raw) < 4:
        return []
    count = struct.unpack_from("<I", raw)[0]
    if count > (len(raw) - 4) // 4:
        return []
    offsets = list(struct.unpack_from(f"<{count}I", raw, 4))
    offsets.append(len(raw))
    out = []
    for i in range(count):
        a, b = offsets[i], offsets[i + 1]
        if a > b or b > len(raw):
            break
        out.append(decode_scalar(base, raw[a:b], cpid))
    return out


class ObjectRef:
    """Value of a PT_OBJECT property: a sub-node id plus a size."""

    __slots__ = ("nid", "size")

    def __init__(self, nid: int, size: int):
        self.nid = nid
        self.size = size

    def __repr__(self):
        return f"ObjectRef(nid=0x{self.nid:x}, size={self.size})"
