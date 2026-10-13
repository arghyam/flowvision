-- PostgreSQL schema for the meter reader, generated from src/service/api/database.py (the service's own table
-- definitions), so it is exactly what the service creates. Running it is optional: the service creates missing tables,
-- columns and indexes itself on every start (under an advisory lock, so several workers can start at once).
-- If a DBA runs it instead, run it as the service's database user: that user must own these tables, because the
-- service checks and extends them on start. See docs/DEPLOY_AWS.md for creating the database and user.
--
-- meter_reading_status: SUCCESS | UNCLEAR | NOMETER | NEEDS_REVIEW | ERROR (request failed) | PROCESSING (in progress)
-- meter_reading: the reading as displayed, e.g. 0019712.7 (returned whatever the status; NULL for NOMETER)
-- image_path: the saved photo, relative to the photo store (METER_IMAGE_DIR); request_json / response_json: the
-- full request (image data left out, URL query removed) and response.

CREATE TABLE IF NOT EXISTS meter_reader_extraction_data (
	request_id VARCHAR NOT NULL, 
	correlation_id VARCHAR, 
	consumer_id VARCHAR, 
	image_url VARCHAR, 
	image_sha1 VARCHAR, 
	request_fingerprint VARCHAR, 
	duplicate_of VARCHAR, 
	image_path VARCHAR, 
	metadata JSONB, 
	request_timestamp TIMESTAMP WITH TIME ZONE, 
	captured_at TIMESTAMP WITH TIME ZONE, 
	meter_reading_status VARCHAR, 
	meter_reading VARCHAR, 
	unit VARCHAR, 
	meter_type VARCHAR, 
	reasons JSONB, 
	result JSONB, 
	model VARCHAR, 
	processing_time FLOAT, 
	cost_usd FLOAT, 
	response_timestamp TIMESTAMP WITH TIME ZONE, 
	extracted_reading_accurate BOOLEAN, 
	actual_reading VARCHAR, 
	feedback_timestamp TIMESTAMP WITH TIME ZONE, 
	request_json JSONB, 
	response_json JSONB, 
	PRIMARY KEY (request_id)
);

CREATE INDEX IF NOT EXISTS ix_meter_reader_extraction_consumer_captured ON meter_reader_extraction_data (consumer_id, captured_at);
CREATE INDEX IF NOT EXISTS ix_meter_reader_extraction_data_consumer_id ON meter_reader_extraction_data (consumer_id);
CREATE INDEX IF NOT EXISTS ix_meter_reader_extraction_data_correlation_id ON meter_reader_extraction_data (correlation_id);
CREATE INDEX IF NOT EXISTS ix_meter_reader_extraction_data_image_sha1 ON meter_reader_extraction_data (image_sha1);
CREATE INDEX IF NOT EXISTS ix_meter_reader_extraction_data_request_timestamp ON meter_reader_extraction_data (request_timestamp);

CREATE TABLE IF NOT EXISTS meter_reader_profiles (
	consumer_id VARCHAR NOT NULL, 
	meter_type_id VARCHAR, 
	decimals INTEGER, 
	digits INTEGER, 
	billing_unit VARCHAR, 
	updated_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (consumer_id)
);

