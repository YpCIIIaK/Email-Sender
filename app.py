import smtplib
import imaplib
import email
import json
import time
import os
import re
import socket
import sqlite3
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
from email.utils import parseaddr
from flask import Flask, render_template, request, redirect, url_for, jsonify, abort
import requests

app = Flask(__name__)
app.config['SECRET_KEY'] = 'change-this-secret'
DB_NAME = 'mailer.db'

# In-memory cancellation registry for running campaigns
CANCELLED_CAMPAIGNS = set()
CANCEL_LOCK = threading.Lock()

EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$')


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_add_column(cur, table, column, ddl):
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError:
        pass


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
    c.execute('''CREATE TABLE IF NOT EXISTS templates
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  name TEXT UNIQUE, subject TEXT, html TEXT, plain TEXT, created_at TEXT)''')
    # Migrations
    _safe_add_column(c, 'campaigns', 'status', "TEXT DEFAULT 'running'")
    conn.commit()
    conn.close()


init_db()


def get_config():
    if os.path.exists('config.json'):
        with open('config.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def parse_recipients(raw_text):
    """Parse recipient list. Each line can be:
       email@x.com         -> {email, name=''}
       email@x.com,Name    -> {email, name}
       Name <email@x.com>  -> {email, name}
    Returns (valid_list, invalid_list)
    """
    valid, invalid = [], []
    seen = set()
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        name = ''
        addr = ''
        if '<' in line and '>' in line:
            n, a = parseaddr(line)
            name, addr = n.strip(), a.strip()
        elif ',' in line:
            parts = line.split(',', 1)
            addr = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else ''
        elif ';' in line:
            parts = line.split(';', 1)
            addr = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else ''
        else:
            addr = line
        if EMAIL_RE.match(addr) and addr.lower() not in seen:
            seen.add(addr.lower())
            valid.append({'email': addr, 'name': name})
        else:
            invalid.append(line)
    return valid, invalid


def render_template_vars(text, recipient):
    if not text:
        return text
    name = recipient.get('name') or recipient.get('email', '').split('@')[0]
    return (text
            .replace('{{name}}', name)
            .replace('{{email}}', recipient.get('email', ''))
            .replace('{{Name}}', name.capitalize() if name else ''))


def check_domain_exists(email_addr):
    """Check if the recipient domain has MX records (can receive email).
       Only MX records matter — A/AAAA records don't mean email works.
    """
    try:
        domain = email_addr.split('@')[1].lower().strip()
        if not domain:
            return False
        import socket as _sock
        _sock.setdefaulttimeout(3)
        try:
            import dns.resolver
            try:
                answers = dns.resolver.resolve(domain, 'MX', lifetime=3)
                return len(answers) > 0
            except dns.resolver.NoAnswer:
                return False
            except dns.resolver.NXDOMAIN:
                return False
            except Exception:
                return False  # Any DNS error = domain likely invalid
        except ImportError:
            return True  # Can't check, let SMTP decide
    except Exception:
        return True


def send_email_sync(config, recipient, html_content, plain_text, attachments_dir=None, attach_files=False):
    # Quick DNS pre-check to skip obviously invalid domains
    if not check_domain_exists(recipient['email']):
        return False, "Domain not found (DNS check failed)"

    try:
        host_val = config.get('smtp_server', 'smtp.gmail.com')
        port_val = int(config.get('smtp_port', 587))
        server = smtplib.SMTP(host_val, port_val, timeout=30)
        server.starttls()
        user_val = config.get('sender_email', '')
        pass_val = config.get('sender_password', '')
        server.login(user_val, pass_val)

        msg = MIMEMultipart('mixed')
        msg['From'] = config.get('sender_email', '')
        msg['To'] = recipient['email']
        msg['Subject'] = render_template_vars(config.get('subject', 'No Subject'), recipient)

        html_personal = render_template_vars(html_content, recipient) if html_content else ''
        plain_personal = render_template_vars(plain_text, recipient) if plain_text else ''

        # Generate plain text from HTML if only HTML is provided
        if html_personal and not plain_personal:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_personal, 'html.parser')
                # Remove script and style elements
                for script_or_style in soup(['script', 'style']):
                    script_or_style.decompose()
                text = soup.get_text()
                # Clean up whitespace
                lines = (line.strip() for line in text.splitlines())
                text = '\n'.join(line for line in lines if line)
                plain_personal = text.strip()
            except ImportError:
                # Fallback to regex if bs4 is not installed
                import re
                text = re.sub(r'<style[^>]*>.*?</style>', '', html_personal, flags=re.DOTALL)
                text = re.sub(r'<[^>]+>', '', text)
                text = text.replace('&nbsp;', ' ').replace('<', '<').replace('>', '>').replace('&', '&')
                plain_personal = text.strip()

        # Embed plain text into HTML so it's visible in email clients
        # Email clients (Gmail, Outlook) ignore separate text/plain part when HTML exists
        if html_personal and plain_personal:
            # Escape plain text for safe HTML embedding
            import html as html_mod
            escaped_plain = html_mod.escape(plain_personal)
            # Convert newlines to <br> for HTML display
            escaped_plain = escaped_plain.replace('\n', '<br>')
            # Inject plain text into <body> so it's visible (not after </html>)
            plain_block = (
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
                '<tr><td style="padding:24px 24px 24px 24px; border-top:2px solid #e0e0e0;">'
                '<div style="font-family:Arial,sans-serif; font-size:13px; color:#555; line-height:1.6; white-space:pre-wrap;">'
                + escaped_plain +
                '</div></td></tr></table>'
            )
            # Insert before </body> or at the end before </html> (case-insensitive, preserve original case)
            import re as re_mod
            if re_mod.search(r'</body>', html_personal, re_mod.IGNORECASE):
                html_personal = re_mod.sub(r'(?i)</body>', plain_block + '\n</body>', html_personal, count=1)
            elif re_mod.search(r'</html>', html_personal, re_mod.IGNORECASE):
                html_personal = re_mod.sub(r'(?i)</html>', plain_block + '\n</html>', html_personal, count=1)
            else:
                html_personal = html_personal + '\n' + plain_block

        # Use multipart/alternative: plain text first (fallback), HTML second (preferred)
        # Both are embedded: plain text is visible inside HTML, and also available as fallback
        alternative = MIMEMultipart('alternative')
        msg.attach(alternative)

        if plain_personal:
            alternative.attach(MIMEText(plain_personal, 'plain', 'utf-8'))
        if html_personal:
            alternative.attach(MIMEText(html_personal, 'html', 'utf-8'))

        if attach_files and attachments_dir and os.path.exists(attachments_dir):
            for filename in os.listdir(attachments_dir):
                filepath = os.path.join(attachments_dir, filename)
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

        # Use sendmail instead of send_message to detect rejected recipients
        # sendmail returns a dict of rejected addresses
        rejected = server.sendmail(
            config.get('sender_email', ''),
            [recipient['email']],
            msg.as_string()
        )
        server.quit()

        if rejected:
            # SMTP server rejected the recipient (e.g. domain not found, user unknown)
            error_details = str(rejected)
            return False, f"Recipient rejected: {error_details}"

        return True, None
    except smtplib.SMTPRecipientsRefused as e:
        # All recipients were refused
        return False, f"All recipients refused: {e}"
    except smtplib.SMTPSenderRefused as e:
        return False, f"Sender refused: {e}"
    except smtplib.SMTPDataError as e:
        return False, f"Data error: {e}"
    except smtplib.SMTPConnectError as e:
        return False, f"Connection error: {e}"
    except smtplib.SMTPAuthenticationError as e:
        return False, f"Auth error: {e}"
    except smtplib.SMTPException as e:
        return False, f"SMTP error: {e}"
    except Exception as e:
        return False, str(e)


def latest_campaign_id():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id FROM campaigns ORDER BY id DESC LIMIT 1")
    row = c.fetchone()
    conn.close()
    return row['id'] if row else None


def detect_bounces():
    """Check inbox for bounce/NDR messages and update email_log from 'sent' to 'failed'."""
    if not os.path.exists('config.json'):
        return
    try:
        config = get_config()
        host_val = config.get('imap_server', 'imap.gmail.com')
        mail = imaplib.IMAP4_SSL(host_val)
        mail.login(config.get('sender_email', ''), config.get('sender_password', ''))
        mail.select('inbox')

        # Search for bounce messages (unseen)
        status, messages = mail.search(None, 'UNSEEN')
        if status != 'OK':
            mail.close()
            mail.logout()
            return

        camp_id = latest_campaign_id()
        if not camp_id:
            mail.close()
            mail.logout()
            return

        # Get all sent emails from the latest campaign (to match bounced addresses)
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT email FROM email_log WHERE campaign_id=? AND status='sent'", (camp_id,))
        sent_emails = [row['email'] for row in c.fetchall()]
        conn.close()

        if not sent_emails:
            mail.close()
            mail.logout()
            return

        bounced_recipients = []

        for msg_id in messages[0].split():
            status, msg_data = mail.fetch(msg_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject = (msg['subject'] or '').lower()
                    sender = (msg['from'] or '').lower()
                    body = ""

                    # Get plain text body
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

                    body_lower = body.lower()

                    # Detect bounce/NDR messages
                    is_bounce = False
                    bounce_keywords = [
                        'mail delivery failed', 'delivery failure', 'undelivered',
                        'undeliverable', 'returned mail', 'bounce',
                        'не доставлено', 'адрес не найден', 'домен не найден',
                        'mail delivery system', 'postmaster', 'mailer-daemon',
                        'delivery status notification', 'failure notice',
                        'message could not be delivered', 'recipient rejected'
                    ]
                    sender_keywords = ['mailer-daemon', 'postmaster', 'mail delivery']

                    # Check subject and body for bounce indicators
                    for kw in bounce_keywords:
                        if kw in subject or kw in body_lower:
                            is_bounce = True
                            break

                    # Check sender
                    for kw in sender_keywords:
                        if kw in sender:
                            is_bounce = True
                            break

                    if not is_bounce:
                        continue

                    # Extract bounced email from body
                    # Common patterns: "Original-Recipient: user@domain.com"
                    # or just the email address appearing in the body
                    found_bounce = set()
                    for sent_email in sent_emails:
                        # Check if this sent email is mentioned in the bounce message
                        if sent_email.lower() in body_lower or sent_email.lower() in subject:
                            found_bounce.add(sent_email)

                    bounced_recipients.extend(found_bounce)

        mail.close()
        mail.logout()

        # Update database: mark bounced emails as 'failed'
        if bounced_recipients:
            bounced_recipients = list(set(bounced_recipients))  # deduplicate
            conn = get_db()
            c = conn.cursor()
            for bounced_email in bounced_recipients:
                c.execute(
                    "UPDATE email_log SET status='failed', error='Bounce detected (delivery failed)' "
                    "WHERE campaign_id=? AND email=? AND status='sent'",
                    (camp_id, bounced_email)
                )
            conn.commit()

            # Update campaign stats
            c.execute(
                "SELECT COUNT(*) AS cnt FROM email_log WHERE campaign_id=? AND status='sent'",
                (camp_id,)
            )
            new_sent = c.fetchone()['cnt']
            c.execute(
                "SELECT COUNT(*) AS cnt FROM email_log WHERE campaign_id=? AND status='failed'",
                (camp_id,)
            )
            new_failed = c.fetchone()['cnt']
            c.execute(
                "UPDATE campaigns SET successful=?, failed=? WHERE id=?",
                (new_sent, new_failed, camp_id)
            )
            conn.commit()
            conn.close()

            print(f"Bounce detection: {len(bounced_recipients)} email(s) marked as failed")

    except Exception as e:
        print(f"Bounce detection error: {e}")


def check_replies_thread():
    if not os.path.exists('config.json'):
        return
    try:
        config = get_config()
        host_val = config.get('imap_server', 'imap.gmail.com')
        mail = imaplib.IMAP4_SSL(host_val)
        mail.login(config.get('sender_email', ''), config.get('sender_password', ''))
        mail.select('inbox')

        status, messages = mail.search(None, 'UNSEEN')
        if status != 'OK':
            return

        camp_id = latest_campaign_id()

        for msg_id in messages[0].split():
            status, msg_data = mail.fetch(msg_id, '(RFC822)')
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject = msg['subject'] or ''
                    sender = msg['from'] or ''
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
                        if api_key:
                            headers = {
                                "Authorization": "Bearer " + api_key,
                                "Content-Type": "application/json"
                            }
                            model = config.get('ai_model', 'google/gemini-flash-1.5')
                            payload = {
                                "model": model,
                                "messages": [{
                                    "role": "user",
                                    "content": "Analyze this email reply. Is it positive (interest), negative (not interested), or OOO (out of office)? Reply with one word only: positive, negative, or ooo. Text: " + body[:1500]
                                }]
                            }
                            url = "https://openrouter.ai/api/v1/chat/completions"
                            response = requests.post(url, json=payload, headers=headers, timeout=15)
                            result = response.json()
                            ai_status = result['choices'][0]['message']['content'].strip().lower().split()[0]
                    except Exception as e:
                        print(f"AI error: {e}")

                    conn = get_db()
                    c = conn.cursor()
                    c.execute("INSERT INTO replies (campaign_id, sender_email, subject, body, ai_status, date) VALUES (?,?,?,?,?,?)",
                              (camp_id, sender, subject, body, ai_status, time.strftime('%Y-%m-%d %H:%M:%S')))
                    conn.commit()
                    conn.close()

                    if ai_status == 'positive':
                        try:
                            token = config.get('telegram_token', '')
                            chat_id = config.get('telegram_chat_id', '')
                            if token and chat_id:
                                text = "New Lead!\nFrom: " + sender + "\nSubject: " + subject
                                url = "https://api.telegram.org/bot" + token + "/sendMessage"
                                requests.post(url, data={"chat_id": chat_id, "text": text}, timeout=10)
                        except Exception as e:
                            print(f"Telegram error: {e}")

        mail.close()
        mail.logout()
    except Exception as e:
        print(f"Reply check error: {e}")


# -------------------- Pages --------------------

@app.route('/')
def index():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM campaigns ORDER BY id DESC LIMIT 25")
    campaigns = [dict(r) for r in c.fetchall()]

    c.execute("SELECT COUNT(*) AS n FROM campaigns")
    n_camp = c.fetchone()['n']
    c.execute("SELECT COALESCE(SUM(successful),0) AS s, COALESCE(SUM(failed),0) AS f, COALESCE(SUM(total),0) AS t FROM campaigns")
    agg = c.fetchone()
    c.execute("SELECT COUNT(*) AS n FROM replies WHERE ai_status='positive'")
    leads = c.fetchone()['n']
    c.execute("SELECT COUNT(*) AS n FROM replies")
    total_replies = c.fetchone()['n']

    # last 14 days chart
    c.execute("""
        SELECT substr(start_time,1,10) AS day,
               SUM(successful) AS sent, SUM(failed) AS failed
        FROM campaigns
        WHERE start_time != ''
        GROUP BY day
        ORDER BY day DESC LIMIT 14
    """)
    chart_rows = [dict(r) for r in c.fetchall()][::-1]
    conn.close()

    stats = {
        'campaigns': n_camp,
        'sent': agg['s'] or 0,
        'failed': agg['f'] or 0,
        'total': agg['t'] or 0,
        'replies': total_replies,
        'leads': leads,
        'success_rate': round(((agg['s'] or 0) / (agg['t'] or 1)) * 100, 1) if agg['t'] else 0,
    }
    return render_template('index.html', campaigns=campaigns, stats=stats, chart_rows=chart_rows)


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

    return render_template('settings.html', config=get_config())


@app.route('/send', methods=['GET', 'POST'])
def send_emails():
    if request.method == 'POST':
        emails_text = request.form['emails']
        html_content = request.form.get('html_content', '')
        plain_text = request.form.get('plain_text', '')
        subject = request.form.get('subject', 'No Subject')

        config = get_config()
        config['subject'] = subject

        html_content = html_content.replace('\r\n', '\n').replace('\r', '\n')
        with open('template.html', 'w', encoding='utf-8', newline='\n') as f:
            f.write(html_content)

        assets_dir = config.get('attachments_dir', 'assets')
        os.makedirs(assets_dir, exist_ok=True)
        for old_file in os.listdir(assets_dir):
            old_path = os.path.join(assets_dir, old_file)
            if os.path.isfile(old_path):
                os.remove(old_path)

        has_attachments = False
        if 'attachments' in request.files:
            for file in request.files.getlist('attachments'):
                if file and file.filename:
                    filepath = os.path.join(assets_dir, file.filename)
                    file.save(filepath)
                    has_attachments = True

        recipients, _invalid = parse_recipients(emails_text)
        if not recipients:
            return redirect(url_for('send_emails'))

        conn = get_db()
        c = conn.cursor()
        c.execute("INSERT INTO campaigns (subject, total, successful, failed, start_time, end_time, status) VALUES (?,?,?,?,?,?, 'running')",
                  (subject, len(recipients), 0, 0, time.strftime('%Y-%m-%d %H:%M:%S'), ''))
        campaign_id = c.lastrowid
        conn.commit()
        conn.close()

        def send_thread():
            successful = 0
            failed = 0
            delay = config.get('delay_seconds', 10)
            cancelled = False

            for i, rcpt in enumerate(recipients):
                with CANCEL_LOCK:
                    if campaign_id in CANCELLED_CAMPAIGNS:
                        cancelled = True
                        CANCELLED_CAMPAIGNS.discard(campaign_id)
                        break

                ok, err = send_email_sync(config, rcpt, html_content, plain_text, assets_dir, has_attachments)
                status = 'sent' if ok else 'failed'
                if ok:
                    successful += 1
                else:
                    failed += 1

                conn2 = get_db()
                c2 = conn2.cursor()
                c2.execute("INSERT INTO email_log (campaign_id, email, status, error, sent_at) VALUES (?,?,?,?,?)",
                           (campaign_id, rcpt['email'], status, err, time.strftime('%Y-%m-%d %H:%M:%S')))
                conn2.commit()
                conn2.close()

                if i < len(recipients) - 1:
                    # interruptible sleep
                    for _ in range(int(delay)):
                        with CANCEL_LOCK:
                            if campaign_id in CANCELLED_CAMPAIGNS:
                                break
                        time.sleep(1)

            final_status = 'cancelled' if cancelled else 'done'
            conn3 = get_db()
            c3 = conn3.cursor()
            c3.execute("UPDATE campaigns SET successful=?, failed=?, end_time=?, status=? WHERE id=?",
                       (successful, failed, time.strftime('%Y-%m-%d %H:%M:%S'), final_status, campaign_id))
            conn3.commit()
            conn3.close()

        threading.Thread(target=send_thread, daemon=True).start()
        return redirect(url_for('progress', campaign_id=campaign_id))

    html_content = ""
    if os.path.exists('template.html'):
        with open('template.html', 'r', encoding='utf-8') as f:
            html_content = f.read()

    cfg = get_config()
    subject = cfg.get('subject', '')

    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT id, name, subject FROM templates ORDER BY id DESC")
    templates_list = [dict(r) for r in c.fetchall()]
    conn.close()

    return render_template('send.html',
                           html_content=html_content,
                           emails_list="",
                           plain_text="",
                           subject=subject,
                           templates_list=templates_list)


@app.route('/progress/<int:campaign_id>')
def progress(campaign_id):
    return render_template('progress.html', campaign_id=campaign_id)


@app.route('/campaigns/<int:campaign_id>')
def campaign_detail(campaign_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
    camp = c.fetchone()
    if not camp:
        conn.close()
        abort(404)
    c.execute("SELECT * FROM email_log WHERE campaign_id=? ORDER BY id", (campaign_id,))
    logs = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('campaign_detail.html', camp=dict(camp), logs=logs)


# -------------------- API --------------------

@app.route('/api/progress/<int:campaign_id>')
def api_progress(campaign_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT email, status, error, sent_at FROM email_log WHERE campaign_id=? ORDER BY id", (campaign_id,))
    logs = c.fetchall()
    c.execute("SELECT total, successful, failed, end_time, status, subject FROM campaigns WHERE id=?", (campaign_id,))
    campaign = c.fetchone()
    conn.close()

    if not campaign:
        return jsonify({'error': 'not found'}), 404

    return jsonify({
        'total': campaign['total'],
        'successful': campaign['successful'],
        'failed': campaign['failed'],
        'is_done': bool(campaign['end_time']),
        'status': campaign['status'] or ('done' if campaign['end_time'] else 'running'),
        'subject': campaign['subject'],
        'processed': len(logs),
        'logs': [{'email': l['email'], 'status': l['status'], 'error': l['error'], 'sent_at': l['sent_at']} for l in logs]
    })


@app.route('/api/cancel/<int:campaign_id>', methods=['POST'])
def api_cancel(campaign_id):
    with CANCEL_LOCK:
        CANCELLED_CAMPAIGNS.add(campaign_id)
    return jsonify({'ok': True})


@app.route('/api/campaign/<int:campaign_id>', methods=['DELETE'])
def api_delete_campaign(campaign_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM email_log WHERE campaign_id=?", (campaign_id,))
    c.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/validate_emails', methods=['POST'])
def api_validate_emails():
    data = request.get_json(silent=True) or {}
    raw = data.get('text', '')
    valid, invalid = parse_recipients(raw)
    return jsonify({'valid': valid, 'invalid': invalid, 'valid_count': len(valid), 'invalid_count': len(invalid)})


@app.route('/api/test_send', methods=['POST'])
def api_test_send():
    data = request.get_json(silent=True) or {}
    to_email = (data.get('to') or '').strip()
    if not EMAIL_RE.match(to_email):
        return jsonify({'ok': False, 'error': 'Invalid email'}), 400
    config = get_config()
    config['subject'] = data.get('subject', config.get('subject', 'Test'))
    rcpt = {'email': to_email, 'name': data.get('name', '')}
    ok, err = send_email_sync(config, rcpt, data.get('html', ''), data.get('plain', ''))
    return jsonify({'ok': ok, 'error': err})


@app.route('/api/templates', methods=['GET', 'POST'])
def api_templates():
    conn = get_db()
    c = conn.cursor()
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        name = (data.get('name') or '').strip()
        if not name:
            return jsonify({'ok': False, 'error': 'name required'}), 400
        try:
            c.execute("INSERT INTO templates (name, subject, html, plain, created_at) VALUES (?,?,?,?,?)",
                      (name, data.get('subject', ''), data.get('html', ''), data.get('plain', ''),
                       time.strftime('%Y-%m-%d %H:%M:%S')))
            conn.commit()
        except sqlite3.IntegrityError:
            c.execute("UPDATE templates SET subject=?, html=?, plain=? WHERE name=?",
                      (data.get('subject', ''), data.get('html', ''), data.get('plain', ''), name))
            conn.commit()
        conn.close()
        return jsonify({'ok': True})
    c.execute("SELECT id, name, subject, created_at FROM templates ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route('/api/templates/<int:tid>', methods=['GET', 'DELETE'])
def api_template_detail(tid):
    conn = get_db()
    c = conn.cursor()
    if request.method == 'DELETE':
        c.execute("DELETE FROM templates WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    c.execute("SELECT * FROM templates WHERE id=?", (tid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    return jsonify(dict(row))


@app.route('/api/reply/<int:rid>')
def api_reply(rid):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM replies WHERE id=?", (rid,))
    row = c.fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'not found'}), 404
    return jsonify(dict(row))


@app.route('/api/reply/<int:rid>', methods=['DELETE'])
def api_reply_delete(rid):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM replies WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# -------------------- Replies --------------------

@app.route('/replies')
def replies():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM replies ORDER BY id DESC")
    replies_rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return render_template('replies.html', replies=replies_rows)


@app.route('/check_replies')
def check_replies():
    threading.Thread(target=check_replies_thread, daemon=True).start()
    return redirect(url_for('replies'))


@app.route('/check_bounces')
def check_bounces():
    threading.Thread(target=detect_bounces, daemon=True).start()
    return redirect(url_for('index'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
