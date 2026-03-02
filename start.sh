#!/bin/bash
# ================================================
# PHP Hosting Bot - Start Script for Render
# PHP install + bot start
# ================================================

echo "🐘 PHP ইনস্টল হচ্ছে..."

# PHP ইনস্টল করুন (Render Ubuntu environment)
apt-get update -qq 2>/dev/null || true
apt-get install -y -qq php php-cli php-cgi php-common php-curl php-mbstring php-xml php-zip php-mysql php-sqlite3 2>/dev/null || true

# PHP version চেক
if command -v php &> /dev/null; then
    echo "✅ PHP ইনস্টল সফল!"
    php -v
else
    echo "⚠️ apt-get কাজ করেনি। Alternative চেষ্টা করছি..."
    # Render এ nix ব্যবহার করুন যদি apt না কাজ করে
    if command -v nix-env &> /dev/null; then
        nix-env -i php 2>/dev/null || true
    fi
fi

# php-cgi check
if command -v php-cgi &> /dev/null; then
    echo "✅ php-cgi পাওয়া গেছে!"
else
    echo "⚠️ php-cgi নেই, php cli ব্যবহার হবে।"
fi

echo ""
echo "🤖 Bot শুরু হচ্ছে..."
python main.py
