"""Named MAPI properties: the name-to-id map of a PST and of a .msg file.

Properties above 0x8000 are not fixed: each file keeps its own table saying
that, say, its 0x8083 means PidLidEmail1EmailAddress (property set
PSETID_Address, id 0x8083). Without that table a contact's e-mail address, a
meeting's location or an item's categories cannot be found.

The table has the same shape in both formats ([MS-PST] 2.4.7, [MS-OXMSG]
2.2.3): a GUID stream (16 bytes each), an entry stream of 8-byte NAMEID
records, and a string stream for properties named by a string rather than a
number. Each NAMEID is

    dwPropertyID   4 bytes  numeric id, or byte offset into the string stream
    wGuid          2 bytes  bit 0: 1 = string name; bits 1-15: GUID index -
                            0 none, 1 PS_MAPI, 2 PS_PUBLIC_STRINGS, n>=3 the
                            (n-3)th GUID in the GUID stream
    wPropIdx       2 bytes  the property id is 0x8000 + wPropIdx

In a PST the three streams are properties 0x0002, 0x0003 and 0x0004 of node
0x61; in a .msg they are the streams __substg1.0_00020102, 00030102 and
00040102 of the __nameid_version1.0 storage.
"""
from __future__ import annotations

import struct
import uuid
from typing import Dict, Optional, Tuple, Union

PS_MAPI = uuid.UUID("00020328-0000-0000-C000-000000000046")
PS_PUBLIC_STRINGS = uuid.UUID("00020329-0000-0000-C000-000000000046")
PSETID_COMMON = uuid.UUID("00062008-0000-0000-C000-000000000046")
PSETID_ADDRESS = uuid.UUID("00062004-0000-0000-C000-000000000046")
PSETID_APPOINTMENT = uuid.UUID("00062002-0000-0000-C000-000000000046")
PSETID_MEETING = uuid.UUID("6ED8DA90-450B-101B-98DA-00AA003F1305")
PSETID_TASK = uuid.UUID("00062003-0000-0000-C000-000000000046")
PSETID_LOG = uuid.UUID("0006200A-0000-0000-C000-000000000046")
PSETID_NOTE = uuid.UUID("0006200E-0000-0000-C000-000000000046")
PSETID_POSTRSS = uuid.UUID("00062041-0000-0000-C000-000000000046")
PSETID_SHARING = uuid.UUID("00062040-0000-0000-C000-000000000046")
PS_INTERNET_HEADERS = uuid.UUID("00020386-0000-0000-C000-000000000046")

SET_NAMES = {
    PS_MAPI: "PS_MAPI", PS_PUBLIC_STRINGS: "PS_PUBLIC_STRINGS", PSETID_COMMON: "PSETID_Common",
    PSETID_ADDRESS: "PSETID_Address", PSETID_APPOINTMENT: "PSETID_Appointment", PSETID_MEETING: "PSETID_Meeting",
    PSETID_TASK: "PSETID_Task", PSETID_LOG: "PSETID_Log", PSETID_NOTE: "PSETID_Note",
    PSETID_POSTRSS: "PSETID_PostRss", PSETID_SHARING: "PSETID_Sharing", PS_INTERNET_HEADERS: "PS_INTERNET_HEADERS",
}

# the contact properties ([MS-OXOCNTC]), all in PSETID_Address
LID_FILE_UNDER = 0x8005
LID_HOME_ADDRESS = 0x801A
LID_WORK_ADDRESS = 0x801B
LID_OTHER_ADDRESS = 0x801C
LID_POSTAL_ADDRESS_ID = 0x8022
LID_WORK_ADDRESS_STREET = 0x8045
LID_WORK_ADDRESS_CITY = 0x8046
LID_WORK_ADDRESS_STATE = 0x8047
LID_WORK_ADDRESS_POSTAL_CODE = 0x8048
LID_WORK_ADDRESS_COUNTRY = 0x8049
LID_IM_ADDRESS = 0x8062
LID_EMAIL1_DISPLAY_NAME = 0x8080
LID_EMAIL1_ADDR_TYPE = 0x8082
LID_EMAIL1_EMAIL_ADDRESS = 0x8083
LID_EMAIL1_ORIGINAL_DISPLAY_NAME = 0x8084
LID_EMAIL2_DISPLAY_NAME = 0x8090
LID_EMAIL2_ADDR_TYPE = 0x8092
LID_EMAIL2_EMAIL_ADDRESS = 0x8093
LID_EMAIL2_ORIGINAL_DISPLAY_NAME = 0x8094
LID_EMAIL3_DISPLAY_NAME = 0x80A0
LID_EMAIL3_ADDR_TYPE = 0x80A2
LID_EMAIL3_EMAIL_ADDRESS = 0x80A3
LID_EMAIL3_ORIGINAL_DISPLAY_NAME = 0x80A4

# canonical names for the ids that turn up in ordinary mailboxes - enough for
# the property inspector to be readable; anything else shows as set + id
KNOWN_NAMES: Dict[Tuple[uuid.UUID, Union[int, str]], str] = {
    (PSETID_ADDRESS, 0x8005): "PidLidFileUnder", (PSETID_ADDRESS, 0x8006): "PidLidFileUnderId",
    (PSETID_ADDRESS, 0x8007): "PidLidContactItemData", (PSETID_ADDRESS, 0x801A): "PidLidHomeAddress",
    (PSETID_ADDRESS, 0x801B): "PidLidWorkAddress", (PSETID_ADDRESS, 0x801C): "PidLidOtherAddress",
    (PSETID_ADDRESS, 0x8022): "PidLidPostalAddressId", (PSETID_ADDRESS, 0x8023): "PidLidContactCharacterSet",
    (PSETID_ADDRESS, 0x8025): "PidLidAutoLog", (PSETID_ADDRESS, 0x8028): "PidLidAddressBookProviderEmailList",
    (PSETID_ADDRESS, 0x8029): "PidLidAddressBookProviderArrayType", (PSETID_ADDRESS, 0x802B): "PidLidHtml",
    (PSETID_ADDRESS, 0x802C): "PidLidYomiFirstName", (PSETID_ADDRESS, 0x802D): "PidLidYomiLastName",
    (PSETID_ADDRESS, 0x802E): "PidLidYomiCompanyName", (PSETID_ADDRESS, 0x8040): "PidLidBusinessCardDisplayDefinition",
    (PSETID_ADDRESS, 0x8041): "PidLidBusinessCardCardPicture", (PSETID_ADDRESS, 0x8045): "PidLidWorkAddressStreet",
    (PSETID_ADDRESS, 0x8046): "PidLidWorkAddressCity", (PSETID_ADDRESS, 0x8047): "PidLidWorkAddressState",
    (PSETID_ADDRESS, 0x8048): "PidLidWorkAddressPostalCode", (PSETID_ADDRESS, 0x8049): "PidLidWorkAddressCountry",
    (PSETID_ADDRESS, 0x804A): "PidLidWorkAddressPostOfficeBox", (PSETID_ADDRESS, 0x804C): "PidLidDistributionListChecksum",
    (PSETID_ADDRESS, 0x804F): "PidLidContactUserField1", (PSETID_ADDRESS, 0x8050): "PidLidContactUserField2",
    (PSETID_ADDRESS, 0x8051): "PidLidContactUserField3", (PSETID_ADDRESS, 0x8052): "PidLidContactUserField4",
    (PSETID_ADDRESS, 0x8053): "PidLidDistributionListName", (PSETID_ADDRESS, 0x8054): "PidLidDistributionListOneOffMembers",
    (PSETID_ADDRESS, 0x8055): "PidLidDistributionListMembers", (PSETID_ADDRESS, 0x8062): "PidLidInstantMessagingAddress",
    (PSETID_ADDRESS, 0x8080): "PidLidEmail1DisplayName", (PSETID_ADDRESS, 0x8082): "PidLidEmail1AddressType",
    (PSETID_ADDRESS, 0x8083): "PidLidEmail1EmailAddress", (PSETID_ADDRESS, 0x8084): "PidLidEmail1OriginalDisplayName",
    (PSETID_ADDRESS, 0x8085): "PidLidEmail1OriginalEntryId", (PSETID_ADDRESS, 0x8090): "PidLidEmail2DisplayName",
    (PSETID_ADDRESS, 0x8092): "PidLidEmail2AddressType", (PSETID_ADDRESS, 0x8093): "PidLidEmail2EmailAddress",
    (PSETID_ADDRESS, 0x8094): "PidLidEmail2OriginalDisplayName", (PSETID_ADDRESS, 0x8095): "PidLidEmail2OriginalEntryId",
    (PSETID_ADDRESS, 0x80A0): "PidLidEmail3DisplayName", (PSETID_ADDRESS, 0x80A2): "PidLidEmail3AddressType",
    (PSETID_ADDRESS, 0x80A3): "PidLidEmail3EmailAddress", (PSETID_ADDRESS, 0x80A4): "PidLidEmail3OriginalDisplayName",
    (PSETID_ADDRESS, 0x80A5): "PidLidEmail3OriginalEntryId", (PSETID_ADDRESS, 0x80B2): "PidLidFax1AddressType",
    (PSETID_ADDRESS, 0x80B3): "PidLidFax1EmailAddress", (PSETID_ADDRESS, 0x80B4): "PidLidFax1OriginalDisplayName",
    (PSETID_ADDRESS, 0x80D8): "PidLidFreeBusyLocation", (PSETID_ADDRESS, 0x80DA): "PidLidHomeAddressCountryCode",
    (PSETID_ADDRESS, 0x80DB): "PidLidWorkAddressCountryCode", (PSETID_ADDRESS, 0x80DD): "PidLidAddressCountryCode",
    (PSETID_ADDRESS, 0x80DE): "PidLidBirthdayLocal", (PSETID_ADDRESS, 0x80DF): "PidLidWeddingAnniversaryLocal",
    (PSETID_ADDRESS, 0x80E0): "PidLidIsContactLinked", (PSETID_ADDRESS, 0x80E2): "PidLidContactLinkedGlobalAddressListEntryId",
    (PSETID_ADDRESS, 0x80E3): "PidLidContactLinkSMTPAddressCache", (PSETID_ADDRESS, 0x80E5): "PidLidContactLinkLinkRejectHistory",
    (PSETID_ADDRESS, 0x80E6): "PidLidContactLinkGlobalAddressListLinkState", (PSETID_ADDRESS, 0x80E8): "PidLidContactLinkGlobalAddressListLinkId",

    (PSETID_COMMON, 0x8501): "PidLidReminderDelta", (PSETID_COMMON, 0x8502): "PidLidReminderTime",
    (PSETID_COMMON, 0x8503): "PidLidReminderSet", (PSETID_COMMON, 0x8506): "PidLidPrivate",
    (PSETID_COMMON, 0x8510): "PidLidSideEffects", (PSETID_COMMON, 0x8514): "PidLidSmartNoAttach",
    (PSETID_COMMON, 0x8516): "PidLidCommonStart", (PSETID_COMMON, 0x8517): "PidLidCommonEnd",
    (PSETID_COMMON, 0x8518): "PidLidTaskMode", (PSETID_COMMON, 0x851F): "PidLidTaskGlobalId",
    (PSETID_COMMON, 0x8520): "PidLidVerbStream", (PSETID_COMMON, 0x8524): "PidLidVerbResponse",
    (PSETID_COMMON, 0x8530): "PidLidFlagRequest", (PSETID_COMMON, 0x8534): "PidLidMileage",
    (PSETID_COMMON, 0x8535): "PidLidBilling", (PSETID_COMMON, 0x8536): "PidLidNonSendableTo",
    (PSETID_COMMON, 0x8537): "PidLidNonSendableCc", (PSETID_COMMON, 0x8538): "PidLidNonSendableBcc",
    (PSETID_COMMON, 0x8539): "PidLidCompanies", (PSETID_COMMON, 0x853A): "PidLidContacts",
    (PSETID_COMMON, 0x8543): "PidLidNonSendToTrackStatus", (PSETID_COMMON, 0x8544): "PidLidNonSendCcTrackStatus",
    (PSETID_COMMON, 0x8545): "PidLidNonSendBccTrackStatus", (PSETID_COMMON, 0x8552): "PidLidCurrentVersion",
    (PSETID_COMMON, 0x8554): "PidLidCurrentVersionName", (PSETID_COMMON, 0x8560): "PidLidReminderSignalTime",
    (PSETID_COMMON, 0x8580): "PidLidInternetAccountName", (PSETID_COMMON, 0x8581): "PidLidInternetAccountStamp",
    (PSETID_COMMON, 0x8582): "PidLidUseTnef", (PSETID_COMMON, 0x8584): "PidLidContactLinkSearchKey",
    (PSETID_COMMON, 0x8585): "PidLidContactLinkEntry", (PSETID_COMMON, 0x8586): "PidLidContactLinkName",
    (PSETID_COMMON, 0x8589): "PidLidSpamOriginalFolder", (PSETID_COMMON, 0x85A0): "PidLidToDoOrdinalDate",
    (PSETID_COMMON, 0x85A1): "PidLidToDoSubOrdinal", (PSETID_COMMON, 0x85A4): "PidLidToDoTitle",
    (PSETID_COMMON, 0x85B1): "PidLidInfoPathFormName", (PSETID_COMMON, 0x85B5): "PidLidClassified",
    (PSETID_COMMON, 0x85B6): "PidLidClassification", (PSETID_COMMON, 0x85BD): "PidLidReferenceEntryId",
    (PSETID_COMMON, 0x85BF): "PidLidValidFlagStringProof", (PSETID_COMMON, 0x85C0): "PidLidFlagString",
    (PSETID_COMMON, 0x85C6): "PidLidConversationActionLastAppliedTime", (PSETID_COMMON, 0x85E0): "PidLidAgingDontAgeMe",

    (PSETID_APPOINTMENT, 0x8201): "PidLidAppointmentSequence", (PSETID_APPOINTMENT, 0x8202): "PidLidAppointmentSequenceTime",
    (PSETID_APPOINTMENT, 0x8203): "PidLidAppointmentLastSequence", (PSETID_APPOINTMENT, 0x8204): "PidLidChangeHighlight",
    (PSETID_APPOINTMENT, 0x8205): "PidLidBusyStatus", (PSETID_APPOINTMENT, 0x8206): "PidLidFExceptionalBody",
    (PSETID_APPOINTMENT, 0x8207): "PidLidAppointmentAuxiliaryFlags", (PSETID_APPOINTMENT, 0x8208): "PidLidLocation",
    (PSETID_APPOINTMENT, 0x8209): "PidLidMeetingWorkspaceUrl", (PSETID_APPOINTMENT, 0x820A): "PidLidForwardInstance",
    (PSETID_APPOINTMENT, 0x820D): "PidLidAppointmentStartWhole", (PSETID_APPOINTMENT, 0x820E): "PidLidAppointmentEndWhole",
    (PSETID_APPOINTMENT, 0x8213): "PidLidAppointmentDuration", (PSETID_APPOINTMENT, 0x8214): "PidLidAppointmentColor",
    (PSETID_APPOINTMENT, 0x8215): "PidLidAppointmentSubType", (PSETID_APPOINTMENT, 0x8216): "PidLidAppointmentRecur",
    (PSETID_APPOINTMENT, 0x8217): "PidLidAppointmentStateFlags", (PSETID_APPOINTMENT, 0x8218): "PidLidResponseStatus",
    (PSETID_APPOINTMENT, 0x8220): "PidLidAppointmentReplyTime", (PSETID_APPOINTMENT, 0x8223): "PidLidRecurring",
    (PSETID_APPOINTMENT, 0x8224): "PidLidIntendedBusyStatus", (PSETID_APPOINTMENT, 0x8228): "PidLidExceptionReplaceTime",
    (PSETID_APPOINTMENT, 0x8229): "PidLidFInvited", (PSETID_APPOINTMENT, 0x822B): "PidLidFExceptionalAttendees",
    (PSETID_APPOINTMENT, 0x8230): "PidLidAppointmentReplyName", (PSETID_APPOINTMENT, 0x8231): "PidLidRecurrenceType",
    (PSETID_APPOINTMENT, 0x8232): "PidLidRecurrencePattern", (PSETID_APPOINTMENT, 0x8233): "PidLidTimeZoneStruct",
    (PSETID_APPOINTMENT, 0x8234): "PidLidTimeZoneDescription", (PSETID_APPOINTMENT, 0x8235): "PidLidClipStart",
    (PSETID_APPOINTMENT, 0x8236): "PidLidClipEnd", (PSETID_APPOINTMENT, 0x8237): "PidLidOriginalStoreEntryId",
    (PSETID_APPOINTMENT, 0x8238): "PidLidAllAttendeesString", (PSETID_APPOINTMENT, 0x823A): "PidLidAutoFillLocation",
    (PSETID_APPOINTMENT, 0x823B): "PidLidToAttendeesString", (PSETID_APPOINTMENT, 0x823C): "PidLidCcAttendeesString",
    (PSETID_APPOINTMENT, 0x823E): "PidLidConferencingCheck", (PSETID_APPOINTMENT, 0x8240): "PidLidConferencingType",
    (PSETID_APPOINTMENT, 0x8241): "PidLidDirectory", (PSETID_APPOINTMENT, 0x8242): "PidLidOrganizerAlias",
    (PSETID_APPOINTMENT, 0x8244): "PidLidAutoStartCheck", (PSETID_APPOINTMENT, 0x8246): "PidLidAllowExternalCheck",
    (PSETID_APPOINTMENT, 0x8247): "PidLidCollaborateDoc", (PSETID_APPOINTMENT, 0x8248): "PidLidNetShowUrl",
    (PSETID_APPOINTMENT, 0x8249): "PidLidOnlinePassword", (PSETID_APPOINTMENT, 0x8250): "PidLidAppointmentProposedStartWhole",
    (PSETID_APPOINTMENT, 0x8251): "PidLidAppointmentProposedEndWhole", (PSETID_APPOINTMENT, 0x8256): "PidLidAppointmentProposedDuration",
    (PSETID_APPOINTMENT, 0x8257): "PidLidAppointmentCounterProposal", (PSETID_APPOINTMENT, 0x8259): "PidLidAppointmentProposalNumber",
    (PSETID_APPOINTMENT, 0x825A): "PidLidAppointmentNotAllowPropose", (PSETID_APPOINTMENT, 0x825E): "PidLidAppointmentTimeZoneDefinitionStartDisplay",
    (PSETID_APPOINTMENT, 0x825F): "PidLidAppointmentTimeZoneDefinitionEndDisplay", (PSETID_APPOINTMENT, 0x8260): "PidLidAppointmentTimeZoneDefinitionRecur",

    (PSETID_MEETING, 0x0001): "PidLidAttendeeCriticalChange", (PSETID_MEETING, 0x0002): "PidLidWhere",
    (PSETID_MEETING, 0x0003): "PidLidGlobalObjectId", (PSETID_MEETING, 0x0004): "PidLidIsSilent",
    (PSETID_MEETING, 0x0005): "PidLidIsRecurring", (PSETID_MEETING, 0x0006): "PidLidRequiredAttendees",
    (PSETID_MEETING, 0x0007): "PidLidOptionalAttendees", (PSETID_MEETING, 0x0008): "PidLidResourceAttendees",
    (PSETID_MEETING, 0x0009): "PidLidDelegateMail", (PSETID_MEETING, 0x000A): "PidLidIsException",
    (PSETID_MEETING, 0x000C): "PidLidTimeZone", (PSETID_MEETING, 0x000D): "PidLidStartRecurrenceDate",
    (PSETID_MEETING, 0x000E): "PidLidStartRecurrenceTime", (PSETID_MEETING, 0x000F): "PidLidEndRecurrenceDate",
    (PSETID_MEETING, 0x0010): "PidLidEndRecurrenceTime", (PSETID_MEETING, 0x001A): "PidLidOwnerCriticalChange",
    (PSETID_MEETING, 0x001C): "PidLidCalendarType", (PSETID_MEETING, 0x0023): "PidLidCleanGlobalObjectId",
    (PSETID_MEETING, 0x0024): "PidLidAppointmentMessageClass", (PSETID_MEETING, 0x0026): "PidLidMeetingType",
    (PSETID_MEETING, 0x0028): "PidLidOldLocation", (PSETID_MEETING, 0x0029): "PidLidOldWhenStartWhole",
    (PSETID_MEETING, 0x002A): "PidLidOldWhenEndWhole",

    (PSETID_TASK, 0x8101): "PidLidTaskStatus", (PSETID_TASK, 0x8102): "PidLidPercentComplete",
    (PSETID_TASK, 0x8103): "PidLidTeamTask", (PSETID_TASK, 0x8104): "PidLidTaskStartDate",
    (PSETID_TASK, 0x8105): "PidLidTaskDueDate", (PSETID_TASK, 0x8107): "PidLidTaskResetReminder",
    (PSETID_TASK, 0x8108): "PidLidTaskAccepted", (PSETID_TASK, 0x8109): "PidLidTaskDeadOccurrence",
    (PSETID_TASK, 0x810F): "PidLidTaskDateCompleted", (PSETID_TASK, 0x8110): "PidLidTaskActualEffort",
    (PSETID_TASK, 0x8111): "PidLidTaskEstimatedEffort", (PSETID_TASK, 0x8112): "PidLidTaskVersion",
    (PSETID_TASK, 0x8113): "PidLidTaskState", (PSETID_TASK, 0x8115): "PidLidTaskLastUpdate",
    (PSETID_TASK, 0x8116): "PidLidTaskRecurrence", (PSETID_TASK, 0x8117): "PidLidTaskAssigners",
    (PSETID_TASK, 0x8119): "PidLidTaskStatusOnComplete", (PSETID_TASK, 0x811A): "PidLidTaskHistory",
    (PSETID_TASK, 0x811B): "PidLidTaskUpdates", (PSETID_TASK, 0x811C): "PidLidTaskComplete",
    (PSETID_TASK, 0x811E): "PidLidTaskFCreator", (PSETID_TASK, 0x811F): "PidLidTaskOwner",
    (PSETID_TASK, 0x8120): "PidLidTaskMultipleRecipients", (PSETID_TASK, 0x8121): "PidLidTaskAssigner",
    (PSETID_TASK, 0x8122): "PidLidTaskLastUser", (PSETID_TASK, 0x8123): "PidLidTaskOrdinal",
    (PSETID_TASK, 0x8124): "PidLidTaskNoCompute", (PSETID_TASK, 0x8125): "PidLidTaskLastDelegate",
    (PSETID_TASK, 0x8126): "PidLidTaskFRecurring", (PSETID_TASK, 0x8127): "PidLidTaskRole",
    (PSETID_TASK, 0x8129): "PidLidTaskOwnership", (PSETID_TASK, 0x812A): "PidLidTaskAcceptanceState",
    (PSETID_TASK, 0x812C): "PidLidTaskFFixOffline", (PSETID_TASK, 0x8139): "PidLidTaskCustomFlags",

    (PSETID_LOG, 0x8700): "PidLidLogType", (PSETID_LOG, 0x8706): "PidLidLogStart",
    (PSETID_LOG, 0x8707): "PidLidLogDuration", (PSETID_LOG, 0x8708): "PidLidLogEnd",
    (PSETID_LOG, 0x870C): "PidLidLogFlags", (PSETID_LOG, 0x870E): "PidLidLogDocumentPrinted",
    (PSETID_LOG, 0x870F): "PidLidLogDocumentSaved", (PSETID_LOG, 0x8710): "PidLidLogDocumentRouted",
    (PSETID_LOG, 0x8711): "PidLidLogDocumentPosted", (PSETID_LOG, 0x8712): "PidLidLogTypeDesc",

    (PSETID_NOTE, 0x8B00): "PidLidNoteColor", (PSETID_NOTE, 0x8B02): "PidLidNoteWidth",
    (PSETID_NOTE, 0x8B03): "PidLidNoteHeight", (PSETID_NOTE, 0x8B04): "PidLidNoteX", (PSETID_NOTE, 0x8B05): "PidLidNoteY",

    (PSETID_POSTRSS, 0x8900): "PidLidPostRssChannelLink", (PSETID_POSTRSS, 0x8901): "PidLidPostRssItemLink",
    (PSETID_POSTRSS, 0x8902): "PidLidPostRssItemHash", (PSETID_POSTRSS, 0x8903): "PidLidPostRssItemGuid",
    (PSETID_POSTRSS, 0x8904): "PidLidPostRssChannel", (PSETID_POSTRSS, 0x8905): "PidLidPostRssItemXml",
    (PSETID_POSTRSS, 0x8906): "PidLidPostRssSubscription",

    (PS_PUBLIC_STRINGS, "keywords"): "PidNameKeywords (Categories)",
}


class NameMap:
    """The resolved name-to-id table of one file.

    `lookup(guid, id_or_name)` gives the property id the file uses (0x8000 and
    up) or None; `name_of(tag)` gives a readable name for the inspector. A map
    that could not be read is still usable - every lookup just says None - and
    says why in `error`."""

    def __init__(self):
        self.by_key: Dict[Tuple[uuid.UUID, Union[int, str]], int] = {}
        self.by_tag: Dict[int, Tuple[uuid.UUID, Union[int, str]]] = {}
        self.error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def __len__(self) -> int:
        return len(self.by_tag)

    def lookup(self, guid: uuid.UUID, ident: Union[int, str]) -> Optional[int]:
        if isinstance(ident, str):
            ident = ident.lower()
        return self.by_key.get((guid, ident))

    def name_of(self, tag: int) -> Optional[str]:
        key = self.by_tag.get(tag)
        if key is None:
            return None
        guid, ident = key
        known = KNOWN_NAMES.get((guid, ident))
        if known:
            return known
        setname = SET_NAMES.get(guid, "{" + str(guid) + "}")
        if isinstance(ident, str):
            return f"{setname}: {ident}"
        return f"{setname} 0x{ident:04X}"

    # -- building ------------------------------------------------------------
    @classmethod
    def from_streams(cls, guids: Optional[bytes], entries: Optional[bytes], strings: Optional[bytes]) -> "NameMap":
        nm = cls()
        guids = guids or b""
        strings = strings or b""
        if entries is None:
            nm.error = "no name-to-id map in this file"
            return nm
        guid_list = [uuid.UUID(bytes_le=bytes(guids[i:i + 16])) for i in range(0, len(guids) - 15, 16)]
        bad = 0
        for off in range(0, len(entries) - 7, 8):
            prop_id, flags, idx = struct.unpack_from("<IHH", entries, off)
            is_string = flags & 1
            gi = flags >> 1
            if gi == 0:
                guid = None
            elif gi == 1:
                guid = PS_MAPI
            elif gi == 2:
                guid = PS_PUBLIC_STRINGS
            elif gi - 3 < len(guid_list):
                guid = guid_list[gi - 3]
            else:
                bad += 1
                continue
            tag = 0x8000 + idx
            if is_string:
                # dwPropertyID is a byte offset into the string stream: 4-byte length, UTF-16LE text
                if prop_id + 4 > len(strings):
                    bad += 1
                    continue
                n = struct.unpack_from("<I", strings, prop_id)[0]
                raw = bytes(strings[prop_id + 4:prop_id + 4 + n])
                if len(raw) % 2:
                    raw = raw[:-1]
                ident: Union[int, str] = raw.decode("utf-16-le", errors="replace").rstrip("\x00").lower()
            else:
                ident = prop_id
            if guid is None:
                guid = PS_MAPI
            nm.by_tag[tag] = (guid, ident)
            nm.by_key.setdefault((guid, ident), tag)
        if bad and not nm.by_tag:
            nm.error = f"name-to-id map unreadable ({bad} bad entries)"
        return nm

    @classmethod
    def from_pst(cls, pst) -> "NameMap":
        """From the NID_NAME_TO_ID_MAP node of an open PSTFile."""
        from .ltp import PropertyContext
        from .ndb import NID_NAME_TO_ID_MAP, PSTError
        try:
            node = pst.ndb.node(NID_NAME_TO_ID_MAP)
            if node is None:
                nm = cls()
                nm.error = "no name-to-id map node in this file"
                return nm
            pc = PropertyContext(node)
            guids = pc.get(0x0002)
            entries = pc.get(0x0003)
            strings = pc.get(0x0004)
            if not isinstance(entries, (bytes, bytearray)):
                nm = cls()
                nm.error = "name-to-id map has no entry stream"
                return nm
            return cls.from_streams(guids if isinstance(guids, (bytes, bytearray)) else b"", bytes(entries),
                                    strings if isinstance(strings, (bytes, bytearray)) else b"")
        except (PSTError, Exception) as exc:  # noqa: BLE001 - a broken map must never stop the file opening
            nm = cls()
            nm.error = f"name-to-id map unreadable: {exc}"
            return nm

    @classmethod
    def from_msg_root(cls, root) -> "NameMap":
        """From the __nameid_version1.0 storage of a .msg's root entry."""
        try:
            st = root.get("__nameid_version1.0")
            if st is None or not st.is_storage:
                nm = cls()
                nm.error = "no __nameid_version1.0 storage in this file"
                return nm

            def stream(name):
                e = st.get(name)
                return e.read() if e is not None and e.is_stream else None

            entries = stream("__substg1.0_00030102")
            if entries is None:
                nm = cls()
                nm.error = "name-to-id storage has no entry stream"
                return nm
            return cls.from_streams(stream("__substg1.0_00020102"), entries, stream("__substg1.0_00040102"))
        except Exception as exc:  # noqa: BLE001
            nm = cls()
            nm.error = f"name-to-id map unreadable: {exc}"
            return nm


def build_streams(entries) -> Tuple[bytes, bytes, bytes]:
    """The inverse, for the test writer: [(guid, id_or_name), ...] in property-index
    order -> (guid stream, entry stream, string stream). Index i becomes 0x8000+i."""
    guid_stream = bytearray()
    guid_index: Dict[uuid.UUID, int] = {}
    entry_stream = bytearray()
    string_stream = bytearray()
    for idx, (guid, ident) in enumerate(entries):
        if guid == PS_MAPI:
            gi = 1
        elif guid == PS_PUBLIC_STRINGS:
            gi = 2
        else:
            if guid not in guid_index:
                guid_index[guid] = len(guid_index)
                guid_stream += guid.bytes_le
            gi = guid_index[guid] + 3
        if isinstance(ident, str):
            off = len(string_stream)
            raw = ident.encode("utf-16-le")
            string_stream += struct.pack("<I", len(raw)) + raw
            while len(string_stream) % 4:
                string_stream += b"\x00"
            entry_stream += struct.pack("<IHH", off, (gi << 1) | 1, idx)
        else:
            entry_stream += struct.pack("<IHH", int(ident), gi << 1, idx)
    return bytes(guid_stream), bytes(entry_stream), bytes(string_stream)
