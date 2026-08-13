# Getting Started

This document provides step-by-step instructions for setting up, configuring, and deploying the FlowVision AI-powered meter reading extraction system. It covers installation of dependencies, environment configuration, service deployment, and initial verification.

For detailed information about the system architecture and component interactions, see [Architecture Overview](architecture-overview.md). For complete API documentation and endpoint details, see [API Reference](api-reference.md).

### Prerequisites <a href="#prerequisites" id="prerequisites"></a>

FlowVision requires the following system components and external services:

#### System Requirements <a href="#system-requirements" id="system-requirements"></a>

| Component  | Requirement        | Purpose                               |
| ---------- | ------------------ | ------------------------------------- |
| Python     | 3.8+               | Core runtime environment              |
| PostgreSQL | 9.6+               | Metadata and request/response storage |
| Redis      | 5.0+               | Rate limiting and caching             |
| AWS S3     | Compatible storage | Image file storage                    |

#### Hardware Requirements <a href="#hardware-requirements" id="hardware-requirements"></a>

* **CPU**: Multi-core processor (8+ cores recommended for concurrent processing)
* **RAM**: 8GB minimum, 16GB+ recommended for ML models
* **GPU**: Optional but recommended for Qwen2-VL local inference (`gpu_type: "cuda"` in config)
* **Storage**: 10GB+ for model files and temporary processing

#### External Service Dependencies <a href="#external-service-dependencies" id="external-service-dependencies"></a>

* **OpenAI API**: Required for GPT-4o vision service (if selected in config)
* **AWS S3**: For image storage with presigned URL generation
* **LocalStack** (development): Local S3-compatible service for testing

### Installation <a href="#installation" id="installation"></a>

#### Environment Setup <a href="#environment-setup" id="environment-setup"></a>

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 12.13.12 PM.png" alt="" width="375"><figcaption></figcaption></figure>

**Setup Workflow with Code Entity Mapping**



#### Step 1: Repository and Environment <a href="#step-1-repository-and-environment" id="step-1-repository-and-environment"></a>

```markdown
# Clone repository
git clone <repository-url>
cd flowvision# 

Create virtual environment
conda create -n flowvision python=3.8
conda activate flowvision

# OR using venv
python3 -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate   # Windows
```

#### Step 2: Install Dependencies <a href="#step-2-install-dependencies" id="step-2-install-dependencies"></a>

Install all required Python packages from the requirements specification:

```
pip install -r requirements.txt
```

Key dependencies installed include:

| Category         | Packages                                                              | Purpose                             |
| ---------------- | --------------------------------------------------------------------- | ----------------------------------- |
| Web Framework    | `fastapi[standard]==0.115.4`, `uvicorn==0.32.0`                       | API server and ASGI runtime         |
| ML/AI            | `torch==2.5.1`, `transformers`, `ultralytics==8.3.91`, `fastai<2.8.0` | Vision model inference              |
| Storage          | `boto3==1.35.54`, `SQLAlchemy==2.0.36`, `psycopg2-binary==2.9.10`     | S3 and database connectivity        |
| Image Processing | `opencv-python==4.11.0.86`, `pillow==11.1.0`                          | Image preprocessing and enhancement |
| Rate Limiting    | `redis==6.0.0`, `fastapi-limiter==0.1.6`                              | Request throttling                  |

### Configuration <a href="#configuration" id="configuration"></a>

#### Configuration Structure <a href="#configuration-structure" id="configuration-structure"></a>

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 12.16.04 PM.png" alt=""><figcaption></figcaption></figure>

**Configuration Structure with YAML Keys**

#### Step 3: Configure config.yaml <a href="#step-3-configure-configyaml" id="step-3-configure-configyaml"></a>

Edit the main configuration file located at `src/conf/config.yaml`:

**Vision Model Selection**

Choose one of the three available vision backends:

```markdown
# For OpenAI GPT-4o (requires OpenAI API key)
vision_model: "gpt-4o"

# For local Qwen2-VL inference (requires GPU)
vision_model: "Qwen/Qwen2-VL-7B-Instruct"
gpu_type: "cuda"  # or "mps" for Apple Silicon

# For InceptionV3 with specialized models (default)
vision_model: "InceptionV3"
```

**Database Configuration**

Configure PostgreSQL connection parameters:

```markdown
database:  
    host: localhost  
    port: 5432  
    dbname: "flowvision"  
    username:   
    password: 
```

**S3 Storage Configuration**

Set up image storage backend:

```markdown
s3:  
    endpoint_url: "http://localhost.localstack.cloud:4566"  # LocalStack for development  
    bucket_name: "flowvision-test-bucket"
presigned_url_expiration: 60  # seconds
```

**ML Model Paths**

Specify paths to pre-trained model files:

```markdown
models:  
    bfm_classification: "/path/to/src/models/bfm_fastai"  
    individual_numbers: "/path/to/src/models/individual_number_recognition_yolo11l.pt"  
    color_classification: "/path/to/src/models/color_classification_fastai"
```

#### Step 4: Environment Variables <a href="#step-4-environment-variables" id="step-4-environment-variables"></a>

Create a `.env` file in the project root with the following variables:

```markdown
# OpenAI API (if using gpt-4o)
OPENAI_API_KEY=your_openai_api_key_here

# AWS S3 ConfigurationAWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
AWS_DEFAULT_REGION=us-east-1

# Database credentials (if different from config.yaml)
POSTGRES_PASSWORD=your_postgres_password

# Application environment
ENVIRONMENT=development
DEBUG=true

# Image quality classification (optional)
# Overrides quality_threshold in config.yaml without a rebuild.
# Float in [0, 1]; higher = stricter (more images returned as UNCLEAR).
FLOWVISION_QUALITY_THRESHOLD=0.65

# Last-digit colour classification (optional)
# Overrides digit_padding_color in config.yaml without a rebuild.
# black | white — the fill colour for the corners of a tilted digit crop that
# fall outside the detected digit polygon. Pair `black` with the v1 colour
# model and `white` with the v2 (finetuned) colour model.
FLOWVISION_DIGIT_PADDING=white
```

### Deployment <a href="#deployment" id="deployment"></a>

#### Step 5: Start Required Services <a href="#step-5-start-required-services" id="step-5-start-required-services"></a>

**PostgreSQL Database**

Create the required database:

```
CREATE DATABASE flowvision;
CREATE USER postgres WITH PASSWORD 'postgres';
GRANT ALL PRIVILEGES ON DATABASE flowvision TO postgres;
```

**Redis Service**

Start Redis for rate limiting:

```
# Using Dockerdocker run -d -p 6379:6379 redis:alpine# Or using system package managersudo systemctl start redis
```

**S3 Storage Setup**

For development with LocalStack:

```markdown
# Start LocalStack
docker run -d -p 4566:4566 localstack/localstack

# Create S3 bucket
aws --endpoint-url=http://localhost:4566 s3 mb s3://flowvision-test-bucket
```

#### Step 6: Launch Application <a href="#step-6-launch-application" id="step-6-launch-application"></a>

Start the FastAPI server using the configured ASGI application:

```
python src/run.py
```

The application will start on the configured port (default: 8000) with the following endpoints exposed:

* `POST /flowvision/v1/uploadImage` - Image upload with S3 storage
* `POST /flowvision/v1/extract-reading` - Meter reading extraction
* `POST /flowvision/v1/feedback` - User feedback logging

### Verification <a href="#verification" id="verification"></a>

#### Step 7: Health Check <a href="#step-7-health-check" id="step-7-health-check"></a>

Verify the system is running correctly:

```markdown
# Check API health
curl http://localhost:8000/health

# Verify rate limiting (Redis connection)
curl http://localhost:8000/flowvision/v1/extract-reading -X POST

# Test image upload
curl -X POST "http://localhost:8000/flowvision/v1/uploadImage" \     -F "image=@path/to/test/image.jpg"
```

#### Service Component Status <a href="#service-component-status" id="service-component-status"></a>

<figure><img src=".gitbook/assets/Screenshot 2025-07-28 at 12.23.10 PM.png" alt="" width="375"><figcaption></figcaption></figure>

**Service Component Health Verification**

#### Test Vision Model Loading <a href="#test-vision-model-loading" id="test-vision-model-loading"></a>

Verify your selected vision model loads correctly:

```markdown
# Check logs for model loading messages
tail -f logs/api_logs/FlowVision.log

# Test with sample image from config
python -c "
from src.conf.config import Config
config = Config()
print(f'Vision model: {config.vision_model}')
print(f'Test image: {config.test.sample_image}')
"
```

### Next Steps <a href="#next-steps" id="next-steps"></a>

After successful deployment:

1. **Configure Vision Service**: Select and configure your preferred vision backend by reviewing [Vision Services](https://deepwiki.com/arghyam/flowvision/3.2-vision-services)
2. **Set up Monitoring**: Configure logging and monitoring as detailed in [Configuration Files](https://deepwiki.com/arghyam/flowvision/4.1-configuration-files)
3. **API Integration**: Begin integrating with the API endpoints documented in [API Endpoints](https://deepwiki.com/arghyam/flowvision/2.1-api-endpoints)
4. **Model Training**: If using custom models, see [Machine Learning Models](https://deepwiki.com/arghyam/flowvision/6-machine-learning-models) for training procedures

For troubleshooting deployment issues, consult [Error Handling](https://deepwiki.com/arghyam/flowvision/7.3-error-handling) for common error codes and resolution steps.
