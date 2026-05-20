# AWS Deployment - Ready to Deploy ✅

Your Phyo backend is now fully configured for AWS deployment with automated CI/CD pipeline.

---

## What's Been Set Up

### ✅ Docker Configuration
- **Server**: Multi-stage Node.js build with production optimizations
- **BrightScraper**: Python 3.11 slim image with health checks
- **docker-compose.yml**: Orchestrates both services with networking and logging

### ✅ CI/CD Pipeline
- **GitHub Actions workflow**: `.github/workflows/deploy-aws.yml`
- Automatically builds and pushes images to AWS ECR on every push to `main`
- Auto-deploys to EC2 instance

### ✅ AWS Infrastructure Templates
- **Dockerfiles** with health checks and security best practices
- **.dockerignore** files for optimized builds
- **docker-compose.yml** with CloudWatch logging
- **AWS_SETUP_STEPS.md** - Complete setup guide (12 detailed steps)

---

## Quick Start - Next Steps

### 1️⃣ **AWS Account Setup** (30 minutes)

Follow the comprehensive guide: `AWS_SETUP_STEPS.md`

Key steps:
```bash
# Step 1: Create ECR Repositories
aws ecr create-repository --repository-name phyo/server --region us-east-1
aws ecr create-repository --repository-name phyo/brightscraper --region us-east-1

# Step 2: Launch EC2 Instance
# Go to AWS Console → EC2 → Launch Instance
# - AMI: Amazon Linux 2 or Ubuntu
# - Type: t3.medium or larger
# - Storage: 50 GB
# - Ports: 22 (SSH), 80, 443, 4000

# Step 3: Install Docker on EC2
# SSH into EC2 and run the installation commands from AWS_SETUP_STEPS.md
```

### 2️⃣ **GitHub Secrets Configuration** (10 minutes)

Go to: GitHub → Your phyo_docker repo → Settings → Secrets and variables → Actions

Add these secrets (get values from AWS):

```
AWS_ACCESS_KEY_ID = ***
AWS_SECRET_ACCESS_KEY = ***
AWS_ACCOUNT_ID = your-12-digit-account-id
AWS_REGION = us-east-1
EC2_HOST = your-ec2-public-ip
EC2_PRIVATE_KEY = (contents of your .pem key file)
MONGODB_URI = your-mongodb-connection-string
JWT_SECRET = (generate: openssl rand -base64 32)
RAZORPAY_KEY_ID = ***
RAZORPAY_KEY_SECRET = ***
GOOGLE_CLIENT_ID = ***
GOOGLE_CLIENT_SECRET = ***
BRIGHT_DATA_API_KEY = ***
REDIS_URL = redis://localhost:6379
AWS_BUCKET_NAME = phyo-uploads
```

### 3️⃣ **Create Production Environment on EC2** (5 minutes)

SSH into your EC2 instance:

```bash
cd /app/phyo

# Create .env.production with your actual values
cat > .env.production << 'EOF'
AWS_ACCOUNT_ID=your-account-id
AWS_REGION=us-east-1
MONGODB_URI=your-mongodb-uri
JWT_SECRET=your-secret-key
RAZORPAY_KEY_ID=your-key
RAZORPAY_KEY_SECRET=your-secret
GOOGLE_CLIENT_ID=your-google-id
GOOGLE_CLIENT_SECRET=your-google-secret
BRIGHT_DATA_API_KEY=your-api-key
REDIS_URL=redis://localhost:6379
AWS_BUCKET_NAME=phyo-uploads
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
EOF
```

### 4️⃣ **Deploy Manually (First Time)** (15 minutes)

SSH into EC2 and deploy:

```bash
cd /app/phyo

# Login to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin {AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com

# Pull latest images
docker compose pull

# Start services
docker compose up -d

# Verify
docker compose ps
docker compose logs -f
```

### 5️⃣ **Configure Nginx + SSL/TLS** (20 minutes)

Follow the Nginx configuration section in `AWS_SETUP_STEPS.md`:

```bash
# Install Nginx
sudo yum install nginx -y
sudo systemctl start nginx

# Get SSL certificate
sudo certbot certonly --nginx -d api.yourdomain.com

# Configure proxy (see AWS_SETUP_STEPS.md for full config)
```

### 6️⃣ **Update DNS Records** (5 minutes)

In your domain registrar (Route 53, GoDaddy, Namecheap, etc.):

```
Type: A
Name: api.yourdomain.com
Value: {EC2_PUBLIC_IP}
```

### 7️⃣ **Push Code to Deploy** (Automatic)

```bash
git push origin main
```

This triggers GitHub Actions which will:
- ✅ Build Docker images
- ✅ Push to ECR
- ✅ SSH into EC2
- ✅ Pull latest images
- ✅ Restart services

---

## Verification

### Check Deployment Status

```bash
# In GitHub Actions tab
# Watch the deploy-aws workflow run

# On EC2
docker compose ps
docker compose logs -f server
docker compose logs -f brightscraper
```

### Test Endpoints

```bash
# Server health
curl https://api.yourdomain.com/health

# Swagger docs
curl https://api.yourdomain.com/api-docs

# BrightScraper (from EC2)
curl http://localhost:5000/health
```

---

## Architecture Overview

```
┌─────────────────┐
│  GitHub Actions │  (CI/CD Pipeline)
│   (on push)     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  AWS ECR        │  (Docker Image Registry)
│  - server       │
│  - brightscraper│
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  AWS EC2        │  (Server Instance)
│  ┌───────────┐  │
│  │ Docker    │  │
│  │ Compose   │  │
│  │ ─────────│  │
│  │ Server   │  │
│  │ :4000    │  │
│  │ ─────────│  │
│  │ Scraper  │  │
│  │ :5000    │  │
│  └───────────┘  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Nginx          │  (Reverse Proxy + SSL)
│  ─────────────  │
│  api.yourdomain │
│  (ports 80/443) │
└─────────────────┘
```

---

## Environment Files

### `.env.production` (on EC2)
- **Location**: `/app/phyo/.env.production`
- **Loaded by**: `docker-compose.yml`
- **Variables**: All sensitive config (DB, API keys, JWT secrets)
- **Security**: Never commit to git, only on EC2

### `.github/workflows/deploy-aws.yml`
- **Auto-triggered**: When code pushed to `main`
- **Builds**: Docker images for both services
- **Pushes**: To ECR with git SHA tags
- **Deploys**: SSHs to EC2 and restarts containers

---

## Monitoring & Logs

### Real-Time Logs
```bash
# SSH into EC2
docker compose logs -f server
docker compose logs -f brightscraper
```

### CloudWatch Logs (configured in compose file)
- `/ecs/phyo-server` - Server logs
- `/ecs/phyo-brightscraper` - Scraper logs

### Container Stats
```bash
docker compose stats
```

---

## Troubleshooting

### Services won't start
```bash
docker compose logs
# Check for env var issues, port conflicts, or permission errors
```

### Can't connect to EC2
```bash
# Verify key permissions
chmod 600 your-key.pem

# Check security group allows port 22
# Verify EC2 instance is in "running" state
```

### Images not pulling from ECR
```bash
# Verify AWS credentials
aws sts get-caller-identity

# Login to ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin {ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com

# Pull again
docker compose pull
```

### Nginx proxy not working
```bash
# Test config
sudo nginx -t

# Check logs
sudo tail -f /var/log/nginx/error.log

# Verify backend services running
docker compose ps
```

---

## Cost Estimation

| Service | Type | Monthly Cost |
|---------|------|--------------|
| EC2 | t3.medium | ~$35 |
| ECR | Image storage | ~$5-10 |
| Data Transfer | Out of AWS | ~$10-20 |
| MongoDB Atlas | Free tier | $0 (for dev) |
| **Total** | | **~$50-65** |

---

## Files Created

```
phyo_docker/
├── .github/
│   └── workflows/
│       └── deploy-aws.yml              # CI/CD workflow
├── server/
│   ├── Dockerfile                      # Server image
│   └── .dockerignore
├── BrightScraper/
│   ├── Dockerfile                      # Scraper image
│   └── .dockerignore
├── docker-compose.yml                  # Service orchestration
├── .env.production                     # Production template
├── AWS_DEPLOYMENT_GUIDE.md             # Detailed guide
├── AWS_SETUP_STEPS.md                  # Step-by-step setup
└── DEPLOYMENT_READY.md                 # This file
```

---

## Next: Frontend Configuration

After backend is deployed, update your frontend `.env.local`:

```
NEXT_PUBLIC_API_URL=https://api.yourdomain.com/api
```

Then rebuild and deploy frontend to Vercel.

---

## Support

If you encounter issues:

1. **Check AWS_SETUP_STEPS.md** - Has troubleshooting section
2. **Review GitHub Actions logs** - Click workflow run to see detailed logs
3. **Check EC2 logs** - SSH in and run `docker compose logs`
4. **Verify environment variables** - Ensure all secrets are set in GitHub

---

## Summary

✅ **Infrastructure**: Ready
✅ **CI/CD**: Ready
✅ **Docker**: Ready
⏳ **AWS Account**: Action needed
⏳ **GitHub Secrets**: Action needed
⏳ **Deployment**: Follow steps above

**Time to first deployment**: ~2 hours (mostly AWS setup)

---

Proceed to: `AWS_SETUP_STEPS.md` for detailed instructions.
