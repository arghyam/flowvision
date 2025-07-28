# Architecture Overview

## Architecture Overview <a href="#architecture-overview" id="architecture-overview"></a>

<details>

<summary>Relevant source files</summary>

*
*
*
*

</details>

This document provides a detailed technical overview of the FlowVision system architecture, including component relationships, data flow patterns, and processing pipelines. It covers the core service architecture, vision processing backends, and integration patterns used throughout the system.

For API-specific details, see [API Reference](https://deepwiki.com/arghyam/flowvision/2-api-reference). For individual service implementations, see [Core Services](https://deepwiki.com/arghyam/flowvision/3-core-services). For configuration management, see [Configuration](https://deepwiki.com/arghyam/flowvision/4-configuration).

### System Overview <a href="#system-overview" id="system-overview"></a>

FlowVision implements a layered microservices architecture with pluggable AI/ML backends for meter reading extraction. The system follows a strategy pattern for vision service selection and uses asynchronous processing for metadata persistence.

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.30.22 PM.png" alt=""><figcaption></figcaption></figure>

### Core Components <a href="#core-components" id="core-components"></a>

#### ImageService - Central Orchestrator <a href="#imageservice---central-orchestrator" id="imageservice---central-orchestrator"></a>

The `ImageService` class serves as the main processing orchestrator, coordinating between vision services, image preprocessing, and metadata persistence.

| Component                | Responsibility                                  | Key Methods                                                   |
| ------------------------ | ----------------------------------------------- | ------------------------------------------------------------- |
| `ImageService`           | Request orchestration, vision service selection | `extract_reading()`, `log_feedback()`                         |
| Vision Service Selection | Strategy pattern implementation                 | Configuration-based initialization                            |
| Image Preprocessing      | Resize, crop, enhancement                       | `resize_image()`, `crop_image()`, `preprocess_image()`        |
| Error Handling           | Exception management and logging                | `handle_custom_http_exception()`, `handle_other_exceptions()` |


#### Configuration Management <a href="#configuration-management" id="configuration-management"></a>

The system uses a hierarchical configuration approach with YAML-based settings and runtime configuration objects.


### Processing Pipeline <a href="#processing-pipeline" id="processing-pipeline"></a>

#### Request Flow Architecture <a href="#request-flow-architecture" id="request-flow-architecture"></a>

The system implements a three-stage processing pipeline: image upload, reading extraction, and feedback collection.

```
"S3 Storage""MetadataStore""inference_utils""Vision Service""ImageService""routes.py"Client"S3 Storage""MetadataStore""inference_utils""Vision Service""ImageService""routes.py"ClientImage Upload PhaseReading Extraction Phasealt[Quality Status == 'good'][Quality Status == 'bad']Feedback Phase"POST /flowvision/v1/uploadImage""StorageService.upload_image()""ImageUploadResponse""Image URL""POST /flowvision/v1/extract-reading""extract_reading(request, background_tasks)""store_request() [async]""preprocess_image(imageURL)""classify_bfm_image()""extract(image_bytes)""meter_reading, boxes, classes""classify_color_image()""store_response() [async]""ReadingExtractionResponse(SUCCESS)""store_response() [async]""ReadingExtractionResponse(UNCLEAR)""Extraction Results""POST /flowvision/v1/feedback""log_feedback(request, background_tasks)""store_feedback() [async]""FeedbackResponse""Feedback Confirmation"
```


#### Image Processing Pipeline <a href="#image-processing-pipeline" id="image-processing-pipeline"></a>

The image processing pipeline implements quality gates and preprocessing steps before AI inference.

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.32.13 PM.png" alt=""><figcaption></figcaption></figure>

**Sources:** [src/service/api/image\_service.py93-101](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/api/image_service.py#L93-L101) [src/service/api/image\_service.py64-91](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/api/image_service.py#L64-L91) [src/service/api/image\_service.py115-172](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/api/image_service.py#L115-L172) [src/service/vision/inference\_utils.py81-122](https://github.com/arghyam/flowvision/blob/78c1136f/src/service/vision/inference_utils.py#L81-L122)

### Vision Service Architecture <a href="#vision-service-architecture" id="vision-service-architecture"></a>

#### Strategy Pattern Implementation <a href="#strategy-pattern-implementation" id="strategy-pattern-implementation"></a>

The system uses a strategy pattern to support multiple AI/ML backends for meter reading extraction, configured via the `vision_model` parameter.

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.33.15 PM.png" alt=""><figcaption></figcaption></figure>


#### Model Integration Patterns <a href="#model-integration-patterns" id="model-integration-patterns"></a>

Each vision service implements different integration patterns for AI/ML model access:

| Service                    | Integration Pattern  | Model Access   | Key Features                 |
| -------------------------- | -------------------- | -------------- | ---------------------------- |
| `OpenAIVisionService`      | API-based            | Remote GPT-4o  | System context, 5-step rules |
| `QwenVisionService`        | Local inference      | Local Qwen2-VL | CUDA/MPS support             |
| `InceptionV3VisionService` | Multi-model pipeline | FastAI + YOLO  | Specialized digit detection  |

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.36.35 PM.png" alt=""><figcaption></figcaption></figure>


### Data Flow and Storage <a href="#data-flow-and-storage" id="data-flow-and-storage"></a>

#### Metadata Persistence Architecture <a href="#metadata-persistence-architecture" id="metadata-persistence-architecture"></a>

The system implements comprehensive audit trails through asynchronous metadata storage for all operations.

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.38.12 PM.png" alt=""><figcaption></figcaption></figure>

#### Configuration Data Flow <a href="#configuration-data-flow" id="configuration-data-flow"></a>

Configuration management follows a hierarchical pattern with YAML files and runtime objects.

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 2.39.11 PM.png" alt=""><figcaption></figcaption></figure>

