// IMAP servers normally honor partial BODY[] fetches, but the protocol parser
// still needs its own hard ceiling in case a broken or hostile server announces
// a larger literal. Keep these constants centralized so fetch, parse, storage,
// attachment download, and API serialization enforce the same budget.
export const INCOMING_SOURCE_MAX_BYTES = 2 * 1024 * 1024;
export const INCOMING_PROTOCOL_MAX_LINE_BYTES = INCOMING_SOURCE_MAX_BYTES;

export const INCOMING_TEXT_MAX_CHARS = 256 * 1024;
export const INCOMING_HTML_MAX_CHARS = 512 * 1024;
export const INCOMING_SUBJECT_MAX_CHARS = 2 * 1024;
export const INCOMING_HEADER_MAX_CHARS = 8 * 1024;
// These three allowlisted RFC/X-header values are safety signals for downstream
// automation, not a general raw-header archive. Their useful values are short;
// the explicit cap is mirrored by varchar(512) columns in the DB schema.
export const INCOMING_AUTOMATION_HEADER_MAX_CHARS = 512;
export const INCOMING_NAME_MAX_CHARS = 512;
export const INCOMING_ADDRESS_MAX_CHARS = 320;
export const INCOMING_CONTENT_TYPE_MAX_CHARS = 255;
export const INCOMING_FILENAME_MAX_CHARS = 255;
export const INCOMING_ADDRESS_MAX_ITEMS = 100;
export const INCOMING_ATTACHMENT_MAX_ITEMS = 100;
export const INCOMING_ATTACHMENT_MAX_BYTES = INCOMING_SOURCE_MAX_BYTES;
