import subprocess
import re
import sys

# এখানে আপনার লোকাল সার্ভারের পোর্ট নম্বরটি দিন (যেটাতে আপনার Flask চলছে)
LOCAL_PORT = 5000 

def make_server_public():
    print(f"⏳ Port {LOCAL_PORT} কে Cloudflare-এর মাধ্যমে পাবলিক করা হচ্ছে (Termux Supported)...")
    
    # Termux-এর cloudflared কমান্ডটি ব্যাকগ্রাউন্ডে রান করানো হচ্ছে
    command = ["cloudflared", "tunnel", "--url", f"http://localhost:{LOCAL_PORT}"]
    
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        
        print("\n" + "="*55)
        print("🌍 Cloudflare Tunnel Started! লিংক জেনারেট হচ্ছে...\nদয়া করে ৫-১০ সেকেন্ড অপেক্ষা করুন...")
        
        url_found = False
        
        # Cloudflare থেকে আসা লগগুলোর ভেতর থেকে পাবলিক লিংকটি খোঁজা হচ্ছে
        for line in process.stdout:
            if "trycloudflare.com" in line and not url_found:
                match = re.search(r'(https://[a-zA-Z0-9-]+\.trycloudflare\.com)', line)
                if match:
                    public_url = match.group(1)
                    print("\n🎉 SUCCESS! আপনার লোকাল সার্ভার এখন পাবলিক (Live)!")
                    print(f"🔗 Public URL: {public_url}")
                    print("="*55 + "\n")
                    print("এই লিংকটি কপি করে পৃথিবীর যেকোনো প্রান্ত থেকে আপনার সার্ভারে ঢুকতে পারবেন।")
                    print("স্ক্রিপ্টটি বন্ধ করতে টার্মিনালে 'Ctrl + C' চাপুন।")
                    url_found = True
                    
    except FileNotFoundError:
        print("\n[!] 'cloudflared' প্যাকেজটি পাওয়া যায়নি!")
        print("👉 এই কোডটি রান করার আগে টার্মিনালে নিচের কমান্ডটি রান করুন:")
        print("pkg install cloudflared")
    except KeyboardInterrupt:
        print("\n[!] আপনি স্ক্রিপ্টটি বন্ধ করেছেন। পাবলিক লিংকটি অফ হয়ে গেছে।")
        if 'process' in locals():
            process.terminate()

if __name__ == '__main__':
    make_server_public()
