# Email Sender

A full-featured web application for sending bulk personalized emails, tracking delivery progress, and managing replies. Built with Flask and designed for cold outreach, marketing campaigns, and automated email workflows.

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![Flask](https://img.shields.io/badge-Flask-3.0+-green.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

## Features

- **📧 Bulk Email Sending** — Send personalized emails to a list of recipients loaded from a CSV file.
- **📎 File Attachments** — Attach files to outgoing emails with automatic MIME type detection.
- **📊 Real-Time Progress Tracking** — Monitor campaign progress with a live-updating progress bar and detailed logs via Server-Sent Events (SSE).
- **🔄 Reply Monitoring** — Connect to recipient IMAP servers to automatically fetch and display replies to sent campaigns.
- **⚙️ Flexible SMTP/IMAP Configuration** — Configure multiple SMTP and IMAP accounts through the web UI or config file.
- **📝 HTML Email Templates** — Write rich HTML emails with a built-in WYSIWYG editor (TinyMCE) and preview functionality.
- **🎨 Modern UI** — Clean, responsive interface built with Tailwind CSS.
- **🔐 Password Security** — App passwords are stored as SHA-256 hashes in the config file.
- **📋 Campaign Management** — View campaign details, per-recipient status (sent/failed), and individual email logs.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Python 3, Flask |
| Frontend | HTML5, Tailwind CSS, TinyMCE, Vanilla JS |
| Email Protocols | SMTP (smtplib), IMAP (imaplib) |
| Task Queue | Threading (background workers) |
| Deployment | Gunicorn (via Procfile for Heroku) |

```