import smtplib
import json
import time
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication

def load_config():
    mode = 'r'
    enc = 'utf-8'
    with open('config.json', mode, encoding=enc) as f:
        return json.load(f)

def load_emails(filepath):
    mode = 'r'
    enc = 'utf-8'
    with open(filepath, mode, encoding=enc) as f:
        return [line.strip() for line in f if line.strip()]

def load_html_template(filepath):
    mode = 'r'
    enc = 'utf-8'
    with open(filepath, mode, encoding=enc) as f:
        return f.read()

def send_email(smtp, msg):
    smtp.send_message(msg)

def main():
    config = load_config()
    
    emails = load_emails(config['emails_file'])
    total = len(emails)
    print(f"Загружено {total} email-ов для рассылки.")
    
    successful = 0
    failed = 0
    failed_emails = []
    html_content = load_html_template(config['html_template'])
    
    print(f"Подключение к {config['smtp_server']}:{config['smtp_port']}...")
    server = smtplib.SMTP(config['smtp_server'], config['smtp_port'])
    server.starttls()
    server.login(config['sender_email'], config['sender_password'])
    print("Авторизация успешна.\n")
    
    start_time = time.time()
    
    for i, recipient in enumerate(emails):
        try:
            msg = MIMEMultipart('related')
            msg['From'] = config['sender_email']
            msg['To'] = recipient
            msg['Subject'] = config['subject']
            
            msg_alternative = MIMEMultipart('alternative')
            msg.attach(msg_alternative)
            
            # Используем переменные для аргументов
            html_type = 'html'
            msg_alternative.attach(MIMEText(html_content, html_type))
            
            assets_dir = config.get('attachments_dir', 'assets')
            if os.path.exists(assets_dir):
                for filename in os.listdir(assets_dir):
                    filepath = os.path.join(assets_dir, filename)
                    if not os.path.isfile(filepath):
                        continue
                    
                    if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')):
                        mode = 'rb'
                        with open(filepath, mode) as img_file:
                            img = MIMEImage(img_file.read())
                            img.add_header('Content-ID', f'<{filename}>')
                            img.add_header('Content-Disposition', 'inline', filename=filename)
                            msg.attach(img)
                    else:
                        mode = 'rb'
                        with open(filepath, mode) as f:
                            part = MIMEApplication(f.read(), Name=filename)
                        part['Content-Disposition'] = f'attachment; filename="{filename}"'
                        msg.attach(part)
            
            send_email(server, msg)
            successful += 1
            print(f"[{i+1}/{total}] ✅ Отправлено: {recipient}")
            
            if i < total - 1:
                time.sleep(config.get('delay_seconds', 10))
                
        except Exception as e:
            failed += 1
            error_msg = str(e)
            failed_emails.append({'email': recipient, 'error': error_msg})
            print(f"[{i+1}/{total}] ❌ Ошибка {recipient}: {error_msg}")
    
    server.quit()
    
    elapsed_time = time.time() - start_time
    print("\n" + "="*50)
    print("📊 СТАТИСТИКА РАССЫЛКИ")
    print("="*50)
    print(f"Всего попыток:    {total}")
    print(f"Успешно:          {successful}")
    print(f"Ошибок:           {failed}")
    print(f"Затрачено времени: {elapsed_time:.2f} сек.")
    print("="*50)
    
    report_content = f"""ОТЧЕТ О РАССЫЛКЕ
Время: {time.strftime('%Y-%m-%d %H:%M:%S')}
Всего: {total}
Успешно: {successful}
Ошибок: {failed}

СПИСОК НЕРАБОЧИХ ПОЧТ:
"""
    if failed_emails:
        for item in failed_emails:
            report_content += f"- {item['email']} (Причина: {item['error']})\n"
    else:
        report_content += "Нет\n"
        
    mode = 'w'
    enc = 'utf-8'
    with open('report.txt', mode, encoding=enc) as f:
        f.write(report_content)
    
    print(f"\nОтчет сохранен в report.txt")

if __name__ == "__main__":
    main()