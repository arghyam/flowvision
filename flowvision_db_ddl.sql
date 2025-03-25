CREATE DATABASE flowvision;
CREATE USER flowvision_user WITH ENCRYPTED PASSWORD 'yourpass';
ALTER DATABASE flowvision OWNER TO flowvision_user;
GRANT ALL PRIVILEGES ON DATABASE flowvision TO flowvision_user;

CREATE TABLE flowvision_extraction_data (
    request_id VARCHAR PRIMARY KEY,
    image_url VARCHAR NOT NULL,
    image_id VARCHAR,
    metadata JSON,
    request_timestamp TIMESTAMP,
    meter_reading_status VARCHAR,
    meter_reading VARCHAR,
    correlation_id VARCHAR,
    response_timestamp TIMESTAMP,
    extracted_reading_accurate BOOL,
    actual_reading VARCHAR,
    feedback_timestamp TIMESTAMP
);

CREATE INDEX idx_metadata_correlation_id ON flowvision_extraction_data(correlation_id);