FROM python:3.10-slim

# ১. সিস্টেম আপডেট এবং PHP ইনস্টল
RUN apt-get update && apt-get install -y \
    php-cgi \
    php-common \
    && rm -rf /var/lib/apt/lists/*

# ২. কাজের ফোল্ডার সেট করা
WORKDIR /app

# ৩. লাইব্রেরি ইনস্টল
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ৪. ফাইল কপি করা
COPY . .

# ৫. বট রান করা (আপনার ফাইলের নাম main.py হলে)
CMD ["python", "main.py"]
