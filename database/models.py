"""SQLite row shapes (spec section 13-14, local mapping)."""
DOCUMENT_FIELDS = ("document_id title file_name file_type version content_hash status allowed_groups".split())
CHUNK_FIELDS = ("chunk_id document_id text title heading_path page_number parent_chunk_id part_numbers error_codes allowed_groups document_version".split())
