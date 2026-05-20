# AWS Deployment Setup Steps

Complete guide to deploy Phyo backend services (Server + BrightScraper) on AWS with automated CI/CD.

---

## Prerequisites

- AWS Account with appropriate permissions
- GitHub account and repositories configured
- Docker and Docker Compose installed locally (for testing)
- AWS CLI installed and configured
- Domain names for both services (optional but recommended)

---

## Step 1: Create ECR Repositories

### Create Server Repository
```bash
aws ecr create-repository \
  --repository-name phyo/server \
  --region us-east-1
```

### Create BrightScraper Repository
```bash
aws ecr create-repository \
  --repository-name phyo/brightscraper \
  --region us-east-1
```

**Save the Repository URIs** - they'll look like:
```
{AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/phyo/server
{AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/phyo/brightscraper
```

---

## Step 2: Set Up EC2 Instance

### Launch EC2 Instance
1. Go to AWS EC2 Console
2. Click "Launch Instances"
3. Configure:
   - **AMI**: Amazon Linux 2 or Ubuntu 22.04 LTS
   - **Instance Type**: t3.medium (or larger)
   - **Storage**: 50 GB GP3
   - **Security Group**: Allow ports:
     - 80 (HTTP)
     - 443 (HTTPS)
     - 4000 (Server)
     - 5000 (BrightScraper) - internal only
     - 22 (SSH)

### Get EC2 Details
```bash
# After instance is running
aws ec2 describe-instances --filters "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[PublicIpAddress,InstanceId,KeyName]' \
  --region us-east-1
```

Save the **Public IP** and **Key Pair Name** for later.

### Install Docker on EC2
```bash
ssh -i your-key.pem ec2-user@your-ec2-public-ip

# Update system
sudo yum update -y

# Install Docker
sudo yum install docker -y
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker ec2-user

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Verify
docker --version
docker-compose --version

# Create application directory
mkdir -p /app/phyo
cd /app/phyo
```

### Configure AWS Credentials on EC2
```bash
mkdir -p ~/.aws

# Create credentials file
cat > ~/.aws/credentials << 'EOF'
[default]
aws_access_key_id = YOUR_ACCESS_KEY
aws_secret_access_key = YOUR_SECRET_KEY
EOF

chmod 600 ~/.aws/credentials

# Create config file
cat > ~/.aws/config << 'EOF'
[default]
region = us-east-1
EOF

# Verify
aws sts get-caller-identity
```

---

## Step 3: Set Up MongoDB (Database)

### Option A: Use MongoDB Atlas (Recommended)
1. Go to [mongodb.com/cloud/atlas](https://mongodb.com/cloud/atlas)
2. Create free cluster
3. Create database user and get connection string
4. Whitelist EC2 public IP (or use 0.0.0.0/0 for testing)
5. Copy connection URI: `mongodb+srv://user:password@cluster.mongodb.net/phyo?retryWrites=true&w=majority`

### Option B: Use AWS DocumentDB
1. Go to AWS DocumentDB Console
2. Create cluster (1 instance minimum)
3. Configure security group for EC2 access
4. Get connection endpoint

---

## Step 4: Set Up Redis (Cache - Optional)

### Using AWS ElastiCache
1. Go to ElastiCache Console
2. Create Redis cluster (cache.t3.micro)
3. Configure security group
4. Save endpoint: `redis://endpoint:6379`

---

## Step 5: Configure GitHub Secrets

### Get AWS Account ID
```bash
aws sts get-caller-identity --query Account --output text
```

### Get EC2 Private Key
```bash
# Download your .pem key from AWS
cat your-key.pem  # Copy entire contents
```

### Add GitHub Secrets

Go to your GitHub repository → Settings → Secrets and variables → Actions

Add these secrets:

| Secret Name | Value |
|------------|-------|
| `AWS_ACCESS_KEY_ID` | Your AWS access key |
| `AWS_SECRET_ACCESS_KEY` | Your AWS secret key |
| `AWS_ACCOUNT_ID` | Your AWS account ID |
| `AWS_REGION` | us-east-1 |
| `EC2_HOST` | EC2 public IP address |
| `EC2_PRIVATE_KEY` | Contents of your .pem key |
| `MONGODB_URI` | MongoDB connection string |
| `JWT_SECRET` | Generate random: `openssl rand -base64 32` |
| `RAZORPAY_KEY_ID` | Your Razorpay key |
| `RAZORPAY_KEY_SECRET` | Your Razorpay secret |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth secret |
| `BRIGHT_DATA_API_KEY` | BrightData API key |
| `REDIS_URL` | Redis connection URL |
| `AWS_BUCKET_NAME` | S3 bucket name for uploads |

---

## Step 6: Set Up Production Environment

### Create `.env.production` on EC2

SSH into EC2 and create environment file:

```bash
cd /app/phyo
cat > .env.production << 'EOF'
AWS_ACCOUNT_ID=your-account-id
AWS_REGION=us-east-1
MONGODB_URI=your-mongodb-uri
JWT_SECRET=your-jwt-secret
RAZORPAY_KEY_ID=your-razorpay-key
RAZORPAY_KEY_SECRET=your-razorpay-secret
GOOGLE_CLIENT_ID=your-google-client-id
GOOGLE_CLIENT_SECRET=your-google-client-secret
GOOGLE_REDIRECT_URI=https://api.yourdomain.com/api/auth/google/callback
BRIGHT_DATA_API_KEY=your-bright-data-key
REDIS_URL=your-redis-url
AWS_BUCKET_NAME=your-bucket-name
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
EOF

chmod 600 .env.production
```

---

## Step 7: Configure Nginx Reverse Proxy

SSH into EC2:

```bash
# Install Nginx
sudo yum install nginx -y
sudo systemctl start nginx
sudo systemctl enable nginx
```

### Configure Server Proxy

Create `/etc/nginx/conf.d/phyo-server.conf`:

```bash
sudo cat > /etc/nginx/conf.d/phyo-server.conf << 'EOF'
upstream phyo_server {
    server localhost:4000;
}

server {
    listen 80;
    server_name api.yourdomain.com;

    location / {
        proxy_pass http://phyo_server;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }
}
EOF
```

### Configure BrightScraper Proxy (Internal Only)

```bash
sudo cat > /etc/nginx/conf.d/phyo-scraper.conf << 'EOF'
upstream phyo_scraper {
    server localhost:5000;
}

server {
    listen 5000;
    server_name localhost;

    location / {
        proxy_pass http://phyo_scraper;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_connect_timeout 300s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
    }
}
EOF
```

### Test and Reload Nginx

```bash
# Test configuration
sudo nginx -t

# Reload
sudo systemctl reload nginx
```

---

## Step 8: Set Up SSL/TLS (HTTPS)

### Install Certbot

```bash
sudo yum install certbot python3-certbot-nginx -y
```

### Get SSL Certificate

```bash
# Replace with your domain
sudo certbot certonly --nginx -d api.yourdomain.com

# Auto-renew
sudo systemctl enable certbot-renew.timer
sudo systemctl start certbot-renew.timer
```

### Update Nginx Config

Update `/etc/nginx/conf.d/phyo-server.conf`:

```bash
sudo cat > /etc/nginx/conf.d/phyo-server.conf << 'EOF'
upstream phyo_server {
    server localhost:4000;
}

server {
    listen 80;
    server_name api.yourdomain.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name api.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/api.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.yourdomain.com/privkey.pem;

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    location / {
        proxy_pass http://phyo_server;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
    }
}
EOF
```

Reload:
```bash
sudo nginx -t && sudo systemctl reload nginx
```

---

## Step 9: Manual First Deployment (Testing)

SSH into EC2:

```bash
cd /app/phyo

# Log into ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin {AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com

# Pull latest images
docker compose pull

# Start services (loads .env.production)
docker compose up -d

# Check status
docker compose ps

# View logs
docker compose logs -f server
docker compose logs -f brightscraper
```

---

## Step 10: Configure GitHub Actions Workflow

The `.github/workflows/deploy-aws.yml` file is already created. It will:

1. Build Docker images on every push to `main`
2. Push to ECR with git SHA tags
3. SSH into EC2 and pull latest images
4. Restart services with new images

### Trigger Deployment

```bash
# Just push to main
git push origin main
```

Or trigger manually in GitHub Actions UI.

---

## Step 11: Configure CloudWatch Logs (Optional)

Create CloudWatch log groups on EC2:

```bash
aws logs create-log-group --log-group-name /ecs/phyo-server --region us-east-1
aws logs create-log-group --log-group-name /ecs/phyo-brightscraper --region us-east-1
```

---

## Step 12: Domain Configuration

### Update DNS Records

For your domain registrar (GoDaddy, Route 53, etc.):

| Type | Name | Value |
|------|------|-------|
| A | api.yourdomain.com | EC2 Public IP |
| CNAME | api.yourdomain.com | api.yourdomain.com (for some registrars) |

---

## Verification Checklist

- [ ] ECR repositories created
- [ ] EC2 instance running with Docker
- [ ] MongoDB/DocumentDB accessible from EC2
- [ ] GitHub secrets configured
- [ ] Nginx proxy configured
- [ ] SSL certificate installed
- [ ] DNS records pointing to EC2
- [ ] First manual deployment successful
- [ ] GitHub Actions workflow running
- [ ] Both services health checks passing
- [ ] APIs responding at https://api.yourdomain.com

---

## Testing Endpoints

```bash
# Test Server
curl https://api.yourdomain.com/health
curl https://api.yourdomain.com/api-docs

# Test BrightScraper (internal/localhost)
curl http://localhost:5000/health
```

---

## Troubleshooting

### Images not pulling
```bash
# SSH into EC2
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin {AWS_ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com
docker compose pull
```

### Services not starting
```bash
docker compose logs server
docker compose logs brightscraper
```

### Port conflicts
```bash
# Check what's using ports
sudo lsof -i :4000
sudo lsof -i :5000
# Kill if needed: sudo kill -9 PID
```

### Nginx issues
```bash
# Test config
sudo nginx -t

# Reload
sudo systemctl reload nginx

# Check logs
sudo tail -f /var/log/nginx/error.log
```

---

## Monitoring

```bash
# SSH into EC2
cd /app/phyo

# Watch services
docker compose stats

# View real-time logs
docker compose logs -f

# Check container health
docker compose ps
```

---

## Auto-Restart on EC2 Reboot

Create a systemd service to restart Docker Compose:

```bash
sudo cat > /etc/systemd/system/phyo-docker.service << 'EOF'
[Unit]
Description=Phyo Docker Compose
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=/app/phyo
ExecStart=/usr/local/bin/docker-compose up -d
ExecStop=/usr/local/bin/docker-compose down
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable phyo-docker.service
```

---

## Next Steps

1. Complete all AWS setup steps above
2. Configure GitHub secrets
3. Update domain names in Nginx configs
4. Push code to main branch to trigger deployment
5. Monitor logs and verify services are running
6. Test APIs from your frontend

---

