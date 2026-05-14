import smtplib
import imaplib
import email
import json
import time
import os
import sqlite3
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
from flask import Flask, render_template, request, redirect, url_for, jsonify
import requests

app = Flask(__name__)
app.config['SECRET_KEY'] = 'change-this-secret'
DB_NAME = 'mailer.db'

def get_db():
    conn = sqlite3.connect(DB_NAME)
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS campaigns
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  subject TEXT, total INTEGER, successful INTEGER, failed INTEGER,
                  start_time TEXT, end_time TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS email_log
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  campaign_id INTEGER, email TEXT, status TEXT, error TEXT, sent_at TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS replies
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  campaign_id INTEGER, sender_email TEXT, subject TEXT,
                  body TEXT, ai_status TEXT, date TEXT)''')
    conn.commit()
    conn.close()

init_db()

def send_email_sync(config, recipient, html_content, plain_text):
    try:
        host_val = config.get('smtp_server', 'smtp.gmail.com')
        port_val = config.get('smtp_port', 587)
        server = smtplib.SMTP(host_val, port_val)
        server.starttls()
        user_val = config.get('sender_email', '')
        pass_val = config.get('sender_password', '')
        server.login(user_val, pass_val)
        
        msg = MIMEMultipart('mixed')
        msg['From'] = config.get('sender_email', '')
        msg['To'] = recipient
        msg['Subject'] = config.get('subject', 'No Subject')
        
        alternative = MIMEMultipart('alternative')
        msg.attach(alternative)
        
        # If both HTML and plain text exist, combine them (plain text below HTML)
        if html_content and plain_text:
            combined_html = html_content + '<hr style="border: 1px solid #ccc; margin: 20px 0;"><div style="font-family: monospace; white-space: pre-wrap; color: #333;">' + plain_text + '</div>'
            alternative.attach(MIMEText(combined_html, 'html', 'utf-8'))
        elif html_content:
            alternative.attach(MIMEText(html_content, 'html', 'utf-8'))
        elif plain_text:
            alternative.attach(MIMEText(plain_text, 'plain', 'utf-8'))
        
        # Attach files only if they were uploaded in this request
        if config.get('attach_files', False):
            assets_dir = config.get('attachments_dir', 'assets')
            if os.path.exists(assets_dir):
                for filename in os.listdir(assets_dir):
                    filepath = os.path.join(assets_dir, filename)
                    if not os.path.isfile(filepath):
                        continue
                    if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')):
                        with open(filepath, 'rb') as img_file:
                            img = MIMEImage(img_file.read())
                            img.add_header('Content-ID', f'<{filename}>')
                            img.add_header('Content-Disposition', 'inline', filename=filename)
                            msg.attach(img)
                    else:
                        with open(filepath, 'rb') as f:
                            part = MIMEApplication(f.read(), Name=filename)
                        part['Content-Disposition'] = f'attachment; filename="{filename}"'
                        msg.attach(part)
        
        server.send_message(msg)
        server.quit()
        return True, None
    except Exception as e:
        return False, str(e)

def check_replies_thread():
    if not os.path.exists('config.json'):
        return
    try:
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        host_val = config.get('imap_server', 'imap.gmail.com')
        mail = imaplib.IMAP4_SSL(host_val)
        user_val = config.get('sender_email', '')
        pass_val = config.get('sender_password', '')
        mail.login(user_val, pass_val)
        mail.select('inbox')
        
        status, messages = mail.search(None, 'UNSEEN')
        if status != 'OK':
            return
            
        for msg_id in messages[0].split():
            status, msg_data = mail.fetch(msg_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject = msg['subject']
                    sender = msg['from']
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            if part.get_content_type() == "text/plain":
                                payload = part.get_payload(decode=True)
                                if payload:
                                    body = payload.decode('utf-8', errors='ignore')
                                break
                    else:
                        payload = msg.get_payload(decode=True)
                        if payload:
                            body = payload.decode('utf-8', errors='ignore')
                    
                    ai_status = "unknown"
                    try:
                        api_key = config.get('openrouter_key', '')
                        headers = {
                            "Authorization": "Bearer " + api_key,
                            "Content-Type": "application/json"
                        }
                        model = config.get('ai_model', 'google/gemini-flash-1.5')
                        payload = {
                            "model": model,
                            "messages": [{
                                "role": "user",
                                "content": "Analyze this email reply. Is it positive (interest), negative (not interested), or OOO (out of office)? Reply with one word: positive, negative, or ooo. Text: " + body[:1000]
                            }]
                        }
                        url = "https://openrouter.ai/api/v1/chat/completions"
                        response = requests.post(url, json=payload, headers=headers, timeout=10)
                        result = response.json()
                        ai_status = result['choices'][0]['message']['content'].strip().lower()
                    except Exception as e:
                        print(f"AI error: {e}")
                    
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("INSERT INTO replies (sender_email, subject, body, ai_status, date) VALUES (?,?,?,?,?)",
                              (sender, subject, body, ai_status, time.strftime('%Y-%m-%d %H:%M:%S')))
                    conn.commit()
                    conn.close()
                    
                    if ai_status == 'positive':
                        try:
                            token = config.get('telegram_token', '')
                            chat_id = config.get('telegram_chat_id', '')
                            text = "New Lead!\nFrom: " + sender + "\nSubject: " + subject
                            url = "https://api.telegram.org/bot" + token + "/sendMessage"
                            requests.post(url, data={"chat_id": chat_id, "text": text})
                        except Exception as e:
                            print(f"Telegram error: {e}")
        
        mail.close()
        mail.logout()
    except Exception as e:
        print(f"Reply check error: {e}")

@app.route('/')
def index():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM campaigns ORDER BY id DESC")
    campaigns = c.fetchall()
    conn.close()
    return render_template('index.html', campaigns=campaigns)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if request.method == 'POST':
        config = {
            'smtp_server': request.form['smtp_server'],
            'smtp_port': int(request.form['smtp_port']),
            'sender_email': request.form['sender_email'],
            'subject': request.form.get('subject', 'Hello'),
            'sender_password': request.form['sender_password'],
            'imap_server': request.form['imap_server'],
            'openrouter_key': request.form['openrouter_key'],
            'ai_model': request.form.get('ai_model', 'google/gemini-flash-1.5'),
            'telegram_token': request.form['telegram_token'],
            'telegram_chat_id': request.form['telegram_chat_id'],
            'delay_seconds': int(request.form['delay_seconds'])
        }
        with open('config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2)
        return redirect(url_for('settings'))
    
    if os.path.exists('config.json'):
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
    else:
        config = {}
    return render_template('settings.html', config=config)

@app.route('/send', methods=['GET', 'POST'])
def send_emails():
    if request.method == 'POST':
        emails_text = request.form['emails']
        html_content = request.form['html_content']
        plain_text = request.form.get('plain_text', '')
        subject = request.form.get('subject', 'No Subject')
        
        with open('config.json', 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        config['subject'] = subject
        
        # Save HTML content - normalize line endings to prevent extra spaces
        html_content = html_content.replace('\r\n', '\n').replace('\r', '\n')
        with open('template.html', 'w', encoding='utf-8', newline='\n') as f:
            f.write(html_content)
        
        assets_dir = config.get('attachments_dir', 'assets')
        os.makedirs(assets_dir, exist_ok=True)
        
        # Clear old attachments first
        for old_file in os.listdir(assets_dir):
            old_path = os.path.join(assets_dir, old_file)
            if os.path.isfile(old_path):
                os.remove(old_path)
        
        # Save only newly uploaded files
        has_attachments = False
        if 'attachments' in request.files:
            for file in request.files.getlist('attachments'):
                if file and file.filename:
                    filepath = os.path.join(assets_dir, file.filename)
                    file.save(filepath)
                    has_attachments = True
        
        # Set flag for attaching files
        config['attach_files'] = has_attachments
        
        emails = [e.strip() for e in emails_text.split('\n') if e.strip()]
        
        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO campaigns (subject, total, successful, failed, start_time, end_time) VALUES (?,?,?,?,?,?)",
                  (subject, len(emails), 0, 0, time.strftime('%Y-%m-%d %H:%M:%S'), ''))
        campaign_id = c.lastrowid
        conn.commit()
        conn.close()
        
        def send_thread():
            successful = 0
            failed = 0
            delay = config.get('delay_seconds', 10)
            
            for i, email_addr in enumerate(emails):
                ok, err = send_email_sync(config, email_addr, html_content, plain_text)
                status = 'sent' if ok else 'failed'
                if ok:
                    successful += 1
                else:
                    failed += 1
                
                conn2 = get_db()
                c2 = conn2.cursor()
                c2.execute("INSERT INTO email_log (campaign_id, email, status, error, sent_at) VALUES (?,?,?,?,?)",
                           (campaign_id, email_addr, status, err, time.strftime('%Y-%m-%d %H:%M:%S')))
                conn2.commit()
                conn2.close()
                
                if i < len(emails) - 1:
                    time.sleep(delay)
            
            conn3 = get_db()
            c3 = conn3.cursor()
            c3.execute("UPDATE campaigns SET successful=?, failed=?, end_time=? WHERE id=?",
                       (successful, failed, time.strftime('%Y-%m-%d %H:%M:%S'), campaign_id))
            conn3.commit()
            conn3.close()
        
        thread = threading.Thread(target=send_thread)
        thread.start()
        
        return redirect(url_for('progress', campaign_id=campaign_id))
    
    html_content = ""
    if os.path.exists('template.html'):
        with open('template.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
    
    subject = ""
    if os.path.exists('config.json'):
        with open('config.json', 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            subject = cfg.get('subject', '')
    
    return render_template('send.html', html_content=html_content, emails_list="", plain_text="", subject=subject)

@app.route('/progress/<int:campaign_id>')
def progress(campaign_id):
    return render_template('progress.html', campaign_id=campaign_id)

@app.route('/api/progress/<int:campaign_id>')
def api_progress(campaign_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT email, status, error, sent_at FROM email_log WHERE campaign_id=? ORDER BY id", (campaign_id,))
    logs = c.fetchall()
    c.execute("SELECT total, successful, failed, end_time FROM campaigns WHERE id=?", (campaign_id,))
    campaign = c.fetchone()
    conn.close()
    
    total = campaign[0] if campaign else 0
    successful = campaign[1] if campaign else 0
    failed = campaign[2] if campaign else 0
    end_time = campaign[3] if campaign else ''
    is_done = end_time != ''
    
    return jsonify({
        'total': total,
        'successful': successful,
        'failed': failed,
        'is_done': is_done,
        'logs': [{'email': l[0], 'status': l[1], 'error': l[2], 'sent_at': l[3]} for l in logs]
    })

@app.route('/replies')
def replies():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM replies ORDER BY id DESC")
    replies = c.fetchall()
    conn.close()
    return render_template('replies.html', replies=replies)

@app.route('/check_replies')
def check_replies():
    thread = threading.Thread(target=check_replies_thread)
    thread.start()
    return redirect(url_for('replies'))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)