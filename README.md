# Overview

### Purpose and Scope <a href="#purpose-and-scope" id="purpose-and-scope"></a>

FlowVision is an AI-powered meter reading extraction service that processes water meter images to automatically extract numeric readings. The system provides a REST API for image upload, reading extraction, and feedback collection, supporting multiple AI vision backends for flexible deployment scenarios.

This document provides a high-level overview of the FlowVision system architecture, core components, and processing workflow. For detailed API documentation, see [API Reference](https://deepwiki.com/arghyam/flowvision/2-api-reference). For in-depth coverage of individual services, see [Core Services](https://deepwiki.com/arghyam/flowvision/3-core-services). For configuration details, see [Configuration](https://deepwiki.com/arghyam/flowvision/4-configuration).

### System Architecture <a href="#system-architecture" id="system-architecture"></a>

FlowVision follows a layered microservices architecture with the `ImageService` class serving as the central orchestrator. The system supports pluggable AI vision backends and maintains comprehensive audit trails through asynchronous metadata storage.

**System Components Architecture**

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 12.09.14 PM.png" alt=""><figcaption></figcaption></figure>

Sources:&#x20;

[src/routes.py14-41](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L14-L41)

[src/service/api/image\_service.py34-56](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/api/image_service.py#L34-L56)

[src/service/vision/inference\_utils.py31-77](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/vision/inference_utils.py#L31-L77)

### Request Processing Flow <a href="#request-processing-flow" id="request-processing-flow"></a>

The meter reading extraction follows a comprehensive pipeline with quality gates, AI processing, and asynchronous metadata persistence. The `ImageService.extract_reading()` method orchestrates the entire workflow.

**Extraction Request Processing Flow**

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 12.09.56 PM.png" alt=""><figcaption></figcaption></figure>

Sources:&#x20;

[src/service/api/image\_service.py103-187](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/api/image_service.py#L103-L187)&#x20;

[src/routes.py31-34](https://github.com/arghyam/flowvision/blob/78c1136f/src/routes.py#L31-L34)&#x20;

[src/service/vision/inference\_utils.py81-122](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/vision/inference_utils.py#L81-L122)

### Core Components <a href="#core-components" id="core-components"></a>

#### ImageService Class <a href="#imageservice-class" id="imageservice-class"></a>

The `ImageService` class serves as the central orchestrator for all meter reading extraction operations. It coordinates between vision services, handles image preprocessing, manages quality gates, and ensures proper metadata persistence.

| Component                  | Purpose                                                        | Key Methods                                                   |
| -------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------- |
| **Vision Model Selection** | Instantiates appropriate vision service based on configuration | `__init__()` with model routing logic                         |
| **Image Preprocessing**    | Resizes, crops, and prepares images for AI processing          | `preprocess_image()`, `resize_image()`, `crop_image()`        |
| **Quality Assessment**     | Prevents expensive AI processing on poor quality images        | `classify_bfm_image()` integration                            |
| **Reading Extraction**     | Orchestrates the complete extraction pipeline                  | `extract_reading()`                                           |
| **Feedback Logging**       | Handles user feedback for model improvement                    | `log_feedback()`                                              |
| **Error Handling**         | Manages custom and system exceptions                           | `handle_custom_http_exception()`, `handle_other_exceptions()` |

Sources: [src/service/api/image\_service.py34-247](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/api/image_service.py#L34-L247)

#### Vision Service Strategy <a href="#vision-service-strategy" id="vision-service-strategy"></a>

FlowVision implements a strategy pattern for vision services, allowing runtime selection between different AI backends based on configuration. Each service implements a common `extract()` interface but uses different underlying models and processing approaches.

| Vision Service               | Model Type                   | Processing Approach                   | Configuration Key             |
| ---------------------------- | ---------------------------- | ------------------------------------- | ----------------------------- |
| **OpenAIVisionService**      | GPT-4o Vision API            | Cloud-based vision-language model     | `vision_model: "gpt-4o"`      |
| **QwenVisionService**        | Qwen2-VL-2B-Instruct         | Local inference with CUDA/MPS support | `vision_model: "qwen"`        |
| **InceptionV3VisionService** | CNN + YOLO + FastAI pipeline | Multi-stage specialized models        | `vision_model: "inceptionv3"` |

The vision service selection logic is implemented in `ImageService.__init__()` with configuration-driven instantiation:

Sources:&#x20;

[src/service/api/image\_service.py42-56](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/api/image_service.py#L42-L56)

#### Data Processing Pipeline <a href="#data-processing-pipeline" id="data-processing-pipeline"></a>

The system implements a sophisticated multi-stage pipeline with quality gates and specialized model routing:

1. **Image Preprocessing**: Resize to maximum 1000x1000, apply configurable cropping, optimize for AI processing
2. **Quality Gate**: BFM (Bulk Flow Meter) classification using FastAI model to filter poor quality images
3. **Vision Processing**: Route to selected AI backend for meter reading extraction
4. **Post-processing**: Color classification for last digit determination, confidence scoring
5. **Metadata Persistence**: Asynchronous storage of request, response, and feedback data

Sources:&#x20;

[src/service/api/image\_service.py93-187](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/api/image_service.py#L93-L187)&#x20;

[src/service/vision/inference\_utils.py36-122](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/vision/inference_utils.py#L36-L122)

### Storage and Configuration <a href="#storage-and-configuration" id="storage-and-configuration"></a>

#### Storage Architecture <a href="#storage-architecture" id="storage-architecture"></a>

FlowVision uses a multi-tier storage approach:

* **AWS S3**: Image storage with presigned URLs and 60-second expiration
* **PostgreSQL**: Structured metadata storage for requests, responses, and feedback
* **Redis**: Rate limiting and session caching

#### Configuration Management <a href="#configuration-management" id="configuration-management"></a>

The system uses a centralized `Config` class that loads from `config.yaml` and environment variables. Key configuration areas include:

* Vision model selection (`vision_model`)
* Image processing parameters (`image_resizing`, `image_crop`)
* Model paths for FastAI and YOLO models
* Storage credentials and endpoints
* Logging configuration

Sources:&#x20;

[src/service/api/image\_service.py35-63](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/api/image_service.py#L35-L63)&#x20;

[src/service/vision/inference\_utils.py31-32](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/vision/inference_utils.py#L31-L32)



Generated by Deepwiki/Devin, edited by Sreechand
