store_request = """
    INSERT INTO flowvision_extraction_data(request_id, image_url, image_id, metadata, request_timestamp)
    VALUES(:request_id, :image_url, :image_id, :metadata, :request_timestamp)
"""
# store_response = """
#     UPDATE flowvision_extraction_data
#     SET (meter_reading_status, meter_reading, correlation_id, response_timestamp) = 
#     (:meter_reading_status, :meter_reading, :correlation_id, :response_timestamp)
#     WHERE request_id = (:request_id)::VARCHAR
# """
store_response = """
    UPDATE flowvision_extraction_data
    SET (
        meter_reading_status, 
        meter_reading, 
        correlation_id, 
        response_timestamp,
        quality_status,
        quality_confidence,
        last_digit_color,
        color_confidence,
        processing_time
    ) = (
        :meter_reading_status, 
        :meter_reading, 
        :correlation_id, 
        :response_timestamp,
        :quality_status,
        :quality_confidence,
        :last_digit_color,
        :color_confidence,
        :processing_time
    )
    WHERE request_id = (:request_id)::VARCHAR
"""

store_feedback = """
    UPDATE flowvision_extraction_data
    SET (extracted_reading_accurate, actual_reading, feedback_timestamp) = 
    (:extracted_reading_accurate, :actual_reading, :feedback_timestamp)
    WHERE correlation_id = (:correlation_id)::VARCHAR
"""