store_request = """
    INSERT INTO metadata_store(request_id, image_url, image_id, metadata, request_timestamp)
    VALUES(:request_id, :image_url, :image_id, :metadata, :request_timestamp)
"""
store_response = """
    UPDATE metadata_store
    SET (meter_reading_status, meter_reading, correlation_id, response_timestamp) = 
    (:meter_reading_status, :meter_reading, :correlation_id, :response_timestamp)
    WHERE request_id = (:request_id)::VARCHAR
"""
store_feedback = """
    UPDATE metadata_store
    SET (feedback, corrected_reading, feedback_timestamp) = 
    (:feedback, :corrected_reading, :feedback_timestamp)
    WHERE correlation_id = (:correlation_id)::VARCHAR
"""