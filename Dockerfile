# প্লে-রাইটের অফিসিয়াল ইমেজ যাতে সব লাইব্রেরি আগে থেকেই থাকে
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# ওয়ার্কিং ডিরেক্টরি
WORKDIR /app

# ফাইল কপি করা
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ব্রাউজার ইন্সটল নিশ্চিত করা
RUN playwright install chromium

COPY . .

# রান করার কমান্ড
CMD ["python", "main.py"]
