# API Reference

This document provides a comprehensive reference for the FlowVision REST API, including endpoint specifications, request/response formats, authentication, and error handling patterns. The API enables external clients to upload images and extract meter readings using AI-powered vision services.

For detailed endpoint documentation, see [API Endpoints](https://deepwiki.com/arghyam/flowvision/2.1-api-endpoints). For complete data model specifications, see [Data Models](https://deepwiki.com/arghyam/flowvision/2.2-data-models). For information about the underlying services, see [Core Services](https://deepwiki.com/arghyam/flowvision/3-core-services).

### API Overview <a href="#api-overview" id="api-overview"></a>

The FlowVision API is built using FastAPI and exposes a RESTful interface at the base path `/flowvision/v1`. The API follows a three-phase workflow: image upload, reading extraction, and optional feedback submission.

#### API Architecture <a href="#api-architecture" id="api-architecture"></a>

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.40.30 PM.png" alt=""><figcaption></figcaption></figure>

**Sources:** [src/routes.py1-41](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L1-L41)

#### Request/Response Flow <a href="#requestresponse-flow" id="requestresponse-flow"></a>

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.40.59 PM.png" alt=""><figcaption></figcaption></figure>

**Sources:** [src/routes.py26-40](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L26-L40)&#x20;

[flowvision\_api\_spec.yml11-122](https://github.com/arghyam/flowvision/blob/78c1136f/flowvision_api_spec.yml#L11-L122)

### Endpoint Specifications <a href="#endpoint-specifications" id="endpoint-specifications"></a>

#### Health Check Endpoint <a href="#health-check-endpoint" id="health-check-endpoint"></a>

| Method | Path | Description               |
| ------ | ---- | ------------------------- |
| GET    | `/`  | Returns API health status |

**Response:**

```
{  
    "message": "Hi, I am the meter reading assistant."
}
```

**Sources:** [src/routes.py21-23](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L21-L23)

#### Image Upload Endpoint <a href="#image-upload-endpoint" id="image-upload-endpoint"></a>

| Method | Path                         | Description                              |
| ------ | ---------------------------- | ---------------------------------------- |
| POST   | `/flowvision/v1/uploadImage` | Upload image to S3 and get presigned URL |

**Request Format:** `multipart/form-data` with `ImageUploadRequest` model

**Response:** Returns S3 presigned URL with 60-second expiry for subsequent extraction requests.

**Sources:** [src/routes.py26-28](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L26-L28)

#### Reading Extraction Endpoint <a href="#reading-extraction-endpoint" id="reading-extraction-endpoint"></a>

| Method | Path                             | Description                               |
| ------ | -------------------------------- | ----------------------------------------- |
| POST   | `/flowvision/v1/extract-reading` | Extract meter reading from uploaded image |

**Request Format:** JSON with `ReadingExtractionRequest` schema

**Required Fields:**

* `id`: Unique request identifier
* `ts`: Timestamp in ISO format
* `imageURL`: S3 URL from upload endpoint
* `metadata`: Additional context data (nullable)

**Response Model:** `ReadingExtractionResponse` with excluded null fields

**Processing Flow:**

1. Validates image URL and metadata
2. Triggers `ImageService.extract_reading()` with background task scheduling
3. Returns structured response with correlation ID for feedback

**Sources:** [src/routes.py31-34](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L31-L34) [flowvision\_api\_spec.yml11-66](https://github.com/arghyam/flowvision/blob/78c1136f/flowvision_api_spec.yml#L11-L66)

#### Feedback Endpoint <a href="#feedback-endpoint" id="feedback-endpoint"></a>

| Method | Path                      | Description                                    |
| ------ | ------------------------- | ---------------------------------------------- |
| POST   | `/flowvision/v1/feedback` | Submit accuracy feedback for extracted reading |

**Request Format:** JSON with `FeedbackRequest` schema

**Required Fields:**

* `id`: Original request identifier
* `ts`: Feedback timestamp
* `correlationId`: From extraction response
* `data`: Feedback data with accuracy flag and readings

**Response Model:** `FeedbackResponse` with excluded null fields

**Sources:** [src/routes.py37-40](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L37-L40) [flowvision\_api\_spec.yml67-122](https://github.com/arghyam/flowvision/blob/78c1136f/flowvision_api_spec.yml#L67-L122)

### Response Status Patterns <a href="#response-status-patterns" id="response-status-patterns"></a>

#### Success Response Structure <a href="#success-response-structure" id="success-response-structure"></a>

All successful API responses follow this pattern:

```markdown
{  
    "id": "request-uuid",  
    "ts": "2024-01-01T00:00:00Z",   
    "responseCode": "OK",  
    "statusCode": 200,
    "errorCode": null,  
    "result": 
        {    
            "status": "SUCCESS|SUBMITTED",
            "correlationId": "correlation-uuid",    
            "data": { /* endpoint-specific data */ }
        }
}
```

#### Error Response Structure <a href="#error-response-structure" id="error-response-structure"></a>

Error responses include detailed error information:

```markdown
{
  "id": "request-uuid",
  "ts": "2024-01-01T00:00:00Z",
  "responseCode": "ERROR", 
  "statusCode": 500,
  "errorCode": {
    "errorCode": "ERR_READING_EXTRACTION_FAILED",
    "errorMsg": "Failed to extract meter reading"
  },
  "result": null
}

```

#### Extraction Status Values <a href="#extraction-status-values" id="extraction-status-values"></a>

The `ExtractReadingStatus` enum defines possible extraction outcomes:

| Status    | Description                        |
| --------- | ---------------------------------- |
| `SUCCESS` | Reading successfully extracted     |
| `NOMETER` | No meter detected in image         |
| `UNCLEAR` | Image quality too poor for reading |
| `INVALID` | Invalid image or processing error  |

**Sources:** [flowvision\_api\_spec.yml196-203](https://github.com/arghyam/flowvision/blob/78c1136f/flowvision_api_spec.yml#L196-L203)

### Background Task Processing <a href="#background-task-processing" id="background-task-processing"></a>

The API uses FastAPI's `BackgroundTasks` for asynchronous operations:

* **Request Logging:** All extraction requests are logged to PostgreSQL via `MetadataStore`
* **Response Logging:** Extraction results and processing metadata are persisted
* **Feedback Storage:** User feedback is stored with correlation to original requests

This pattern ensures API responses return quickly while maintaining comprehensive audit trails.

**Sources:**&#x20;

[src/routes.py2](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L2-L2)&#x20;

[src/routes.py32](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L32-L32)&#x20;

[src/routes.py38](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L38-L38)

### Service Dependencies <a href="#service-dependencies" id="service-dependencies"></a>

The FastAPI application initializes core services at startup:

| Service          | Purpose                                        | Configuration                              |
| ---------------- | ---------------------------------------------- | ------------------------------------------ |
| `ImageService`   | Orchestrates meter reading extraction workflow | Uses `Config` for vision service selection |
| `StorageService` | Manages S3 image uploads and presigned URLs    | Configured via AWS credentials             |
| `Config`         | Centralized configuration management           | Loads from `config.yaml` and environment   |

**Sources:** [src/routes.py15-18](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L15-L18)
