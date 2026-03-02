#!/bin/bash
# ================================================
# PHP Hosting Bot - Start Script for Render
# Uses Static PHP Binary (No Root Needed)
# ================================================

echo "🐘 PHP ইনস্টল হচ্ছে (Static Binary)..."

# লোকাল বিন ফোল্ডার তৈরি
mkdir -p bin

# PHP ডাউনলোড করা (যদি আগে থেকে না থাকে)
# আমরা এখানে পোর্টেবল PHP 8.2 ব্যবহার করছি যা রুট ছাড়াই চলে
if [ ! -f "bin/php" ]; then
    echo "📥 Downloading PHP..."
    curl -fsSL -o bin/php https://dl.static-php.dev/static/php/8.2/bin/linux/x64/php
    chmod +x bin/php
fi

# সিস্টেম পাথে আমাদের লোকাল বিন ফোল্ডার যুক্ত করা
export PATH="$PWD/bin:$PATH"

# PHP ইনস্টলেশন চেক
if command -v php &> /dev/null; then
    echo "✅ PHP ইনস্টল সফল!"
    php -v
else
    echo "❌ PHP ডাউনলোড ব্যর্থ হয়েছে। ইন্টারনেট কানেকশন চেক করুন।"
fi

# php-cgi চেক (বট যাতে কনফিউজড না হয়)
if ! command -v php-cgi &> /dev/null; then
    echo "⚠️ php-cgi নেই, তবে মূল PHP CLI দিয়েই কাজ চলবে।"
fi

echo ""
echo "🤖 Bot শুরু হচ্ছে..."
python main.py
