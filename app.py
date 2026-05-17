import os
import sys
import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, session

print("="*60, flush=True)
print("STARTING AI ASSISTANT WITH GMAIL REPLY + CALENDAR", flush=True)
print("="*60, flush=True)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
print(f"OPENAI_API_KEY: {'FOUND' if OPENAI_API_KEY else 'MISSING'}", flush=True)

app = Flask(__name__)
app.secret_key = "default-secret-key-12345"

openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        print("✅ OpenAI initialized", flush=True)
    except Exception as e:
        print(f"❌ OpenAI error: {e}", flush=True)

# Storage
tasks = []
notes = []
gmail_creds = {'email': '', 'password': ''}
cached_emails = []
meetings = []

def fetch_emails_imap(email_addr, password):
    """Fetch emails using IMAP"""
    import socket
    socket.setdefaulttimeout(15)
    
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(email_addr, password)
        mail.select("inbox")
        
        status, messages = mail.search(None, "ALL")
        if status != "OK":
            return []
        
        email_ids = messages[0].split()
        email_ids = email_ids[-15:]
        email_ids.reverse()
        
        emails_list = []
        for idx, email_id in enumerate(email_ids):
            try:
                status, msg_data = mail.fetch(email_id, "(RFC822)")
                if status != "OK":
                    continue
                
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        
                        subject = msg["Subject"] or "No Subject"
                        if subject:
                            decoded = decode_header(subject)[0]
                            if isinstance(decoded[0], bytes):
                                subject = decoded[0].decode(decoded[1] or 'utf-8', errors='ignore')
                        
                        sender = msg["From"] or "Unknown"
                        sender_email = sender.split('<')[-1].rstrip('>') if '<' in sender else sender
                        sender_name = sender.split('<')[0].strip().strip('"') if '<' in sender else sender
                        
                        body = ""
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    try:
                                        body = part.get_payload(decode=True).decode('utf-8', errors='ignore')
                                        break
                                    except:
                                        pass
                        else:
                            try:
                                body = msg.get_payload(decode=True).decode('utf-8', errors='ignore')
                            except:
                                body = str(msg.get_payload())
                        
                        emails_list.append({
                            'id': idx,
                            'message_id': msg.get('Message-ID', ''),
                            'sender': sender_name[:50],
                            'sender_email': sender_email,
                            'subject': subject[:100],
                            'preview': body[:300].strip() if body else subject[:300],
                            'full_body': body[:2000] if body else ''
                        })
            except Exception as e:
                print(f"Email parse error: {e}", flush=True)
                continue
        
        mail.logout()
        return emails_list
    except Exception as e:
        print(f"IMAP error: {type(e).__name__}: {e}", flush=True)
        return [{'error': f'{type(e).__name__}: {str(e)}'}]

def send_email_smtp(to_email, subject, body, in_reply_to=None):
    """Send email via Resend API (HTTPS) - works on any platform including free tiers"""
    if not gmail_creds.get('email'):
        return {'success': False, 'error': 'Gmail not connected'}
    
    RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
    
    # Try Resend first (HTTPS-based, works on free tiers)
    if RESEND_API_KEY:
        try:
            import requests as req_lib
            response = req_lib.post(
                'https://api.resend.com/emails',
                headers={
                    'Authorization': f'Bearer {RESEND_API_KEY}',
                    'Content-Type': 'application/json'
                },
                json={
                    'from': f"{gmail_creds['email'].split('@')[0]} <onboarding@resend.dev>",
                    'to': [to_email],
                    'subject': subject,
                    'text': body,
                    'reply_to': gmail_creds['email']
                },
                timeout=15
            )
            if response.status_code in [200, 201]:
                return {'success': True}
            else:
                error_data = response.json() if response.text else {}
                return {'success': False, 'error': f"Resend: {error_data.get('message', response.text[:200])}"}
        except Exception as e:
            print(f"Resend error: {e}", flush=True)
            # Fall through to SMTP
    
    # Fallback to SMTP (only works on paid Render or local)
    try:
        msg = MIMEMultipart()
        msg['From'] = gmail_creds['email']
        msg['To'] = to_email
        msg['Subject'] = subject
        if in_reply_to:
            msg['In-Reply-To'] = in_reply_to
            msg['References'] = in_reply_to
        
        msg.attach(MIMEText(body, 'plain'))
        
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15)
        server.login(gmail_creds['email'], gmail_creds['password'])
        server.send_message(msg)
        server.quit()
        
        return {'success': True}
    except Exception as e:
        print(f"SMTP error: {e}", flush=True)
        error_str = str(e)
        if 'unreachable' in error_str.lower() or 'timeout' in error_str.lower():
            return {'success': False, 'error': 'SMTP blocked on this hosting platform. Add RESEND_API_KEY to environment variables. Get free key at https://resend.com/api-keys'}
        return {'success': False, 'error': error_str}

def generate_ai_reply(email_data, instruction=""):
    """Generate AI reply to an email"""
    if not openai_client:
        return "OpenAI not configured"
    
    prompt = f"""You are helping reply to this email:

From: {email_data.get('sender')}
Subject: {email_data.get('subject')}
Body: {email_data.get('full_body', email_data.get('preview'))}

{f'User instruction: {instruction}' if instruction else 'Generate a professional, friendly reply.'}

Write ONLY the reply body, no subject line, no signature."""
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful email assistant. Generate professional, concise email replies."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=400
        )
        return response.choices[0].message.content
    except Exception as e:
        return f"Error: {str(e)[:200]}"

HTML = '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Assistant - Gmail + Calendar</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; font-family: Arial, sans-serif; min-height: 100vh; padding: 20px; }
.container { max-width: 1100px; margin: 0 auto; background: #1a1a2e; border-radius: 10px; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.3); }
header { padding: 20px; text-align: center; border-bottom: 2px solid #667eea; background: rgba(0,0,0,0.3); }
h1 { color: #667eea; margin-bottom: 10px; }
.status { padding: 8px 16px; border-radius: 20px; font-size: 12px; font-weight: bold; display: inline-block; margin-top: 10px; }
.status.ok { background: #1a4d1a; color: #4ade80; }
.status.error { background: #4d1a1a; color: #ff6b6b; }
.tabs { display: flex; border-bottom: 1px solid #333; background: #0f0f1e; flex-wrap: wrap; }
.tab { flex: 1; min-width: 100px; padding: 15px; background: none; border: none; color: #999; cursor: pointer; font-size: 14px; }
.tab:hover { color: #fff; }
.tab.active { color: #667eea; border-bottom: 3px solid #667eea; }
.content { padding: 20px; }
.page { display: none; }
.page.active { display: block; }
.chatbox { height: 350px; background: #0f0f1e; border: 1px solid #333; border-radius: 5px; padding: 15px; overflow-y: auto; margin-bottom: 15px; }
.msg { margin: 10px 0; padding: 10px; border-radius: 5px; word-wrap: break-word; }
.msg-user { background: #667eea; text-align: right; margin-left: 60px; }
.msg-ai { background: #333; margin-right: 60px; white-space: pre-wrap; }
.input-box { display: flex; gap: 10px; }
input, textarea { flex: 1; padding: 12px; background: #0f0f1e; border: 1px solid #333; color: #fff; border-radius: 5px; font-family: inherit; font-size: 14px; }
input:focus, textarea:focus { outline: none; border-color: #667eea; }
button { padding: 12px 24px; background: #667eea; color: #fff; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; }
button:hover { background: #764ba2; }
button.secondary { background: #4ade80; }
button.secondary:hover { background: #22c55e; }
button.danger { background: #ff6b6b; }
.email-item { background: #0f0f1e; padding: 15px; margin: 10px 0; border-left: 3px solid #667eea; border-radius: 5px; cursor: pointer; transition: all 0.2s; }
.email-item:hover { background: #252540; transform: translateX(5px); }
.email-from { color: #667eea; font-weight: bold; }
.email-subject { margin: 8px 0; font-weight: bold; }
.email-preview { color: #999; font-size: 13px; line-height: 1.5; }
.email-actions { margin-top: 10px; display: flex; gap: 10px; }
.email-actions button { padding: 6px 12px; font-size: 12px; }
.task-item, .note-item { background: #0f0f1e; padding: 12px; margin: 10px 0; border-left: 3px solid #667eea; border-radius: 5px; }
.note-item h4 { color: #667eea; margin-bottom: 5px; }
h2 { color: #667eea; margin: 15px 0; }
h3 { color: #667eea; margin: 10px 0; }
.loading { color: #999; font-style: italic; }
.setup-box { background: #252540; padding: 20px; border-radius: 5px; margin: 15px 0; border: 1px solid #667eea; }
.setup-box ol { margin-left: 20px; line-height: 1.8; }
.setup-box a { color: #667eea; }
.gmail-form { background: #0f0f1e; padding: 20px; border-radius: 5px; margin-bottom: 20px; }
.gmail-form input { margin: 8px 0; }
.gmail-form button { width: 100%; margin-top: 10px; }
.modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 1000; align-items: center; justify-content: center; padding: 20px; }
.modal.active { display: flex; }
.modal-content { background: #1a1a2e; border-radius: 10px; padding: 25px; max-width: 600px; width: 100%; max-height: 90vh; overflow-y: auto; }
.modal-content h3 { margin-bottom: 15px; }
.modal-content textarea { min-height: 120px; margin: 10px 0; }
.modal-actions { display: flex; gap: 10px; margin-top: 15px; }
.modal-actions button { flex: 1; }
.meeting-item { background: #0f0f1e; padding: 15px; margin: 10px 0; border-left: 3px solid #4ade80; border-radius: 5px; }
.meeting-title { color: #4ade80; font-weight: bold; }
.meeting-time { color: #999; font-size: 13px; margin: 5px 0; }
.warning-box { background: #4d1a1a; border-left: 3px solid #ff6b6b; padding: 12px; margin: 10px 0; border-radius: 5px; font-size: 13px; }
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>🤖 AI Assistant + Gmail + Calendar</h1>
        <div id="status"></div>
    </header>
    
    <div class="tabs">
        <button class="tab active" data-page="chat">💬 Chat</button>
        <button class="tab" data-page="gmail">📧 Gmail</button>
        <button class="tab" data-page="calendar">📅 Calendar</button>
        <button class="tab" data-page="tasks">✅ Tasks</button>
        <button class="tab" data-page="notes">📝 Notes</button>
    </div>
    
    <div class="content">
        <div id="chat" class="page active">
            <h2>Chat with AI</h2>
            <div class="warning-box">
                💡 <strong>What I Can Do (Full AI Email Agent):</strong><br>
                📥 <strong>Read:</strong> "summarize my emails", "show emails from amazon"<br>
                ↩️ <strong>Reply:</strong> "reply to john saying thanks", "send reply to emirates"<br>
                ✉️ <strong>Compose:</strong> "send email to boss@company.com about leave request"<br>
                📅 <strong>Schedule:</strong> "schedule meeting with arun@gmail.com tomorrow 3pm about project"<br>
                ✅ <strong>Confirm:</strong> Reply "send", "edit", or "cancel" to confirm actions
            </div>
            <div class="chatbox" id="chatbox"></div>
            <div class="input-box">
                <input type="text" id="chat-input" placeholder="Ask anything...">
                <button id="send-btn">Send</button>
            </div>
        </div>
        
        <div id="gmail" class="page">
            <h2>Gmail Inbox</h2>
            <div class="setup-box">
                <strong>Setup:</strong>
                <ol>
                    <li>Enable 2FA: <a href="https://myaccount.google.com/security" target="_blank">myaccount.google.com/security</a></li>
                    <li>Get App Password: <a href="https://myaccount.google.com/apppasswords" target="_blank">myaccount.google.com/apppasswords</a></li>
                </ol>
            </div>
            <div class="gmail-form" id="gmail-form">
                <input type="email" id="gmail-email" placeholder="your-email@gmail.com">
                <input type="password" id="gmail-password" placeholder="16-character app password">
                <button id="connect-btn">Connect Gmail</button>
            </div>
            <div style="margin: 15px 0;">
                <button id="refresh-btn" style="display:none;">🔄 Refresh Emails</button>
            </div>
            <div id="emails"></div>
        </div>
        
        <div id="calendar" class="page">
            <h2>Meetings & Calendar</h2>
            <div class="warning-box">
                📌 Meetings are stored in this app. To send real calendar invites, you'd need Google Calendar API integration.
            </div>
            <div style="margin: 15px 0;">
                <h3>Schedule New Meeting</h3>
                <input type="text" id="meeting-title" placeholder="Meeting title" style="margin-bottom: 8px;">
                <input type="text" id="meeting-with" placeholder="With (email or name)" style="margin-bottom: 8px;">
                <input type="datetime-local" id="meeting-time" style="margin-bottom: 8px;">
                <textarea id="meeting-notes" placeholder="Notes/Agenda..." style="height: 80px; margin-bottom: 8px;"></textarea>
                <button id="schedule-btn" style="width: 100%;">Schedule Meeting</button>
            </div>
            <h3>Upcoming Meetings</h3>
            <div id="meetings-list"></div>
        </div>
        
        <div id="tasks" class="page">
            <h2>Tasks</h2>
            <div class="input-box">
                <input type="text" id="task-input" placeholder="New task...">
                <button id="task-btn">Add</button>
            </div>
            <div id="task-list"></div>
        </div>
        
        <div id="notes" class="page">
            <h2>Notes</h2>
            <input type="text" id="note-title" placeholder="Title..." style="margin-bottom: 10px;">
            <textarea id="note-content" placeholder="Content..." style="height: 100px; margin-bottom: 10px;"></textarea>
            <button id="note-btn" style="width: 100%;">Save Note</button>
            <div id="note-list" style="margin-top: 15px;"></div>
        </div>
    </div>
</div>

<!-- Reply Modal -->
<div class="modal" id="reply-modal">
    <div class="modal-content">
        <h3>Reply to Email</h3>
        <div id="reply-context" style="background: #252540; padding: 10px; border-radius: 5px; margin-bottom: 10px; font-size: 13px; color: #999;"></div>
        <input type="text" id="reply-instruction" placeholder="How should I reply? (e.g., 'politely decline', 'accept and confirm', or leave blank for auto)">
        <button id="generate-reply-btn" style="width: 100%; margin: 10px 0;">🤖 Generate AI Reply</button>
        <textarea id="reply-body" placeholder="Your reply will appear here..."></textarea>
        <div class="modal-actions">
            <button id="send-reply-btn" class="secondary">📤 Send Reply</button>
            <button id="close-reply-btn" class="danger">Cancel</button>
        </div>
    </div>
</div>

<script>
var currentEmailReply = null;

// Tab switching
document.querySelectorAll('.tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
        var page = this.getAttribute('data-page');
        document.querySelectorAll('.page').forEach(function(p) { p.classList.remove('active'); });
        document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
        document.getElementById(page).classList.add('active');
        this.classList.add('active');
        
        if (page === 'tasks') loadTasks();
        if (page === 'notes') loadNotes();
        if (page === 'calendar') loadMeetings();
    });
});

function checkStatus() {
    fetch('/api/status').then(function(r) { return r.json(); }).then(function(d) {
        var s = document.getElementById('status');
        s.innerHTML = '';
        if (d.openai) {
            s.innerHTML += '<span class="status ok">✅ OpenAI</span> ';
        } else {
            s.innerHTML += '<span class="status error">❌ No OpenAI</span> ';
        }
        if (d.gmail) {
            s.innerHTML += '<span class="status ok">✅ Gmail (' + d.gmail_email + ')</span>';
            document.getElementById('gmail-form').style.display = 'none';
            document.getElementById('refresh-btn').style.display = 'block';
        } else {
            s.innerHTML += '<span class="status error">❌ Gmail not connected</span>';
        }
    });
}

document.getElementById('send-btn').addEventListener('click', sendChat);
document.getElementById('chat-input').addEventListener('keypress', function(e) {
    if (e.key === 'Enter') sendChat();
});

function sendChat() {
    var input = document.getElementById('chat-input');
    var msg = input.value.trim();
    if (!msg) return;
    
    var box = document.getElementById('chatbox');
    var userDiv = document.createElement('div');
    userDiv.className = 'msg msg-user';
    userDiv.textContent = msg;
    box.appendChild(userDiv);
    input.value = '';
    box.scrollTop = box.scrollHeight;
    
    var typingDiv = document.createElement('div');
    typingDiv.className = 'msg msg-ai loading';
    typingDiv.id = 'typing-msg';
    typingDiv.textContent = 'Thinking...';
    box.appendChild(typingDiv);
    box.scrollTop = box.scrollHeight;
    
    fetch('/api/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: msg})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        var t = document.getElementById('typing-msg');
        if (t) box.removeChild(t);
        var aiDiv = document.createElement('div');
        aiDiv.className = 'msg msg-ai';
        aiDiv.textContent = d.response;
        box.appendChild(aiDiv);
        box.scrollTop = box.scrollHeight;
    });
}

document.getElementById('connect-btn').addEventListener('click', function() {
    var email = document.getElementById('gmail-email').value.trim();
    var password = document.getElementById('gmail-password').value.trim();
    
    if (!email || !password) {
        alert('Please enter both email and app password');
        return;
    }
    
    var emailsDiv = document.getElementById('emails');
    emailsDiv.innerHTML = '<p class="loading">Connecting to Gmail (this may take 15 seconds)...</p>';
    
    fetch('/api/gmail-connect', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({email: email, password: password})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.success) {
            checkStatus();
            displayEmails(d.emails);
        } else {
            emailsDiv.innerHTML = '<p style="color: #ff6b6b;">❌ Error: ' + d.error + '</p>' +
                '<div class="warning-box">⚠️ If on Hugging Face: IMAP port 993 is blocked. Deploy on Render.com or run locally.</div>';
        }
    });
});

document.getElementById('refresh-btn').addEventListener('click', function() {
    var emailsDiv = document.getElementById('emails');
    emailsDiv.innerHTML = '<p class="loading">Refreshing...</p>';
    fetch('/api/emails').then(function(r) { return r.json(); }).then(function(d) {
        displayEmails(d.emails);
    });
});

function displayEmails(emails) {
    var div = document.getElementById('emails');
    if (!emails || emails.length === 0) {
        div.innerHTML = '<p class="loading">No emails found</p>';
        return;
    }
    if (emails[0] && emails[0].error) {
        div.innerHTML = '<p style="color: #ff6b6b;">Error: ' + emails[0].error + '</p>';
        return;
    }
    div.innerHTML = emails.map(function(e, i) {
        return '<div class="email-item">' +
            '<div class="email-from">' + e.sender + ' &lt;' + e.sender_email + '&gt;</div>' +
            '<div class="email-subject">' + e.subject + '</div>' +
            '<div class="email-preview">' + e.preview + '</div>' +
            '<div class="email-actions">' +
            '<button onclick="openReply(' + i + ')" class="secondary">↩️ Reply with AI</button>' +
            '<button onclick="summarizeEmail(' + i + ')">📝 Summarize</button>' +
            '</div>' +
            '</div>';
    }).join('');
}

function openReply(emailIdx) {
    fetch('/api/email/' + emailIdx).then(function(r) { return r.json(); }).then(function(d) {
        if (!d.email) return;
        currentEmailReply = d.email;
        document.getElementById('reply-context').innerHTML = 
            '<strong>To:</strong> ' + d.email.sender + '<br>' +
            '<strong>Subject:</strong> Re: ' + d.email.subject;
        document.getElementById('reply-body').value = '';
        document.getElementById('reply-instruction').value = '';
        document.getElementById('reply-modal').classList.add('active');
    });
}

document.getElementById('generate-reply-btn').addEventListener('click', function() {
    if (!currentEmailReply) return;
    var instruction = document.getElementById('reply-instruction').value;
    var btn = this;
    btn.textContent = '🤖 Generating...';
    btn.disabled = true;
    
    fetch('/api/generate-reply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({email_id: currentEmailReply.id, instruction: instruction})
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        document.getElementById('reply-body').value = d.reply;
        btn.textContent = '🤖 Generate AI Reply';
        btn.disabled = false;
    });
});

document.getElementById('send-reply-btn').addEventListener('click', function() {
    if (!currentEmailReply) return;
    var body = document.getElementById('reply-body').value.trim();
    if (!body) {
        alert('Please write or generate a reply');
        return;
    }
    
    var btn = this;
    btn.textContent = '📤 Sending...';
    btn.disabled = true;
    
    fetch('/api/send-reply', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
            to: currentEmailReply.sender_email,
            subject: 'Re: ' + currentEmailReply.subject,
            body: body,
            message_id: currentEmailReply.message_id
        })
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
        if (d.success) {
            alert('✅ Reply sent successfully!');
            document.getElementById('reply-modal').classList.remove('active');
        } else {
            alert('❌ Failed: ' + d.error + '\\n\\nNote: Hugging Face blocks SMTP. Deploy on Render.com or run locally.');
        }
        btn.textContent = '📤 Send Reply';
        btn.disabled = false;
    });
});

document.getElementById('close-reply-btn').addEventListener('click', function() {
    document.getElementById('reply-modal').classList.remove('active');
});

function summarizeEmail(idx) {
    fetch('/api/summarize/' + idx).then(function(r) { return r.json(); }).then(function(d) {
        alert('Summary:\\n\\n' + d.summary);
    });
}

// Calendar
document.getElementById('schedule-btn').addEventListener('click', function() {
    var title = document.getElementById('meeting-title').value.trim();
    var with_ = document.getElementById('meeting-with').value.trim();
    var time = document.getElementById('meeting-time').value;
    var notes = document.getElementById('meeting-notes').value.trim();
    
    if (!title || !time) {
        alert('Please enter title and time');
        return;
    }
    
    fetch('/api/meetings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, with_: with_, time: time, notes: notes})
    }).then(function(r) { return r.json(); }).then(function(d) {
        document.getElementById('meeting-title').value = '';
        document.getElementById('meeting-with').value = '';
        document.getElementById('meeting-time').value = '';
        document.getElementById('meeting-notes').value = '';
        loadMeetings();
        
        if (with_) {
            if (confirm('Meeting scheduled! Send invitation email to ' + with_ + '?')) {
                sendMeetingInvite(title, with_, time, notes);
            }
        }
    });
});

function sendMeetingInvite(title, to, time, notes) {
    fetch('/api/send-meeting-invite', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: title, to: to, time: time, notes: notes})
    }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.success) {
            alert('✅ Invite sent to ' + to);
        } else {
            alert('❌ Failed: ' + d.error);
        }
    });
}

function loadMeetings() {
    fetch('/api/meetings').then(function(r) { return r.json(); }).then(function(d) {
        var list = document.getElementById('meetings-list');
        list.innerHTML = d.meetings.map(function(m) {
            return '<div class="meeting-item">' +
                '<div class="meeting-title">' + m.title + '</div>' +
                '<div class="meeting-time">🕒 ' + m.time + (m.with_ ? ' with ' + m.with_ : '') + '</div>' +
                (m.notes ? '<div style="color: #999; font-size: 13px;">' + m.notes + '</div>' : '') +
                '</div>';
        }).join('') || '<p class="loading">No meetings scheduled</p>';
    });
}

// Tasks
document.getElementById('task-btn').addEventListener('click', addTask);
document.getElementById('task-input').addEventListener('keypress', function(e) {
    if (e.key === 'Enter') addTask();
});

function addTask() {
    var input = document.getElementById('task-input');
    var task = input.value.trim();
    if (!task) return;
    fetch('/api/tasks', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({task: task})
    }).then(function(r) { return r.json(); }).then(function(d) {
        input.value = '';
        loadTasks();
    });
}

function loadTasks() {
    fetch('/api/tasks').then(function(r) { return r.json(); }).then(function(d) {
        document.getElementById('task-list').innerHTML = d.tasks.map(function(t, i) {
            return '<div class="task-item">' + (i+1) + '. ' + t + '</div>';
        }).join('') || '<p class="loading">No tasks</p>';
    });
}

document.getElementById('note-btn').addEventListener('click', function() {
    var t = document.getElementById('note-title').value.trim();
    var c = document.getElementById('note-content').value.trim();
    if (!t || !c) return;
    fetch('/api/notes', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({title: t, content: c})
    }).then(function(r) { return r.json(); }).then(function(d) {
        document.getElementById('note-title').value = '';
        document.getElementById('note-content').value = '';
        loadNotes();
    });
});

function loadNotes() {
    fetch('/api/notes').then(function(r) { return r.json(); }).then(function(d) {
        document.getElementById('note-list').innerHTML = d.notes.map(function(n) {
            return '<div class="note-item"><h4>' + n.title + '</h4><p>' + n.content + '</p></div>';
        }).join('') || '<p class="loading">No notes</p>';
    });
}

checkStatus();
setInterval(checkStatus, 10000);
</script>
</body>
</html>'''

@app.route('/')
def index():
    return render_template_string(HTML)

@app.route('/api/status')
def api_status():
    return jsonify({
        'openai': openai_client is not None,
        'gmail': bool(gmail_creds.get('email')),
        'gmail_email': gmail_creds.get('email', '')
    })

# Conversation state - tracks what the agent is doing
conversation_state = {
    'history': [],  # Chat history
    'pending_action': None,  # What action is pending
    'pending_data': {},  # Data for pending action
}

@app.route('/api/chat', methods=['POST'])
def api_chat():
    msg = request.json.get('message', '')
    
    if not openai_client:
        return jsonify({'response': 'OpenAI not configured. Add OPENAI_API_KEY to environment.'})
    
    msg_lower = msg.lower().strip()
    
    # Check if user is asking about emails but Gmail not connected
    email_keywords = ['email', 'mail', 'inbox', 'message', 'summarize', 'sender', 'amazon', 'reply', 'send', 'gmail']
    is_email_related = any(w in msg_lower for w in email_keywords)
    
    if is_email_related and not gmail_creds.get('email'):
        return jsonify({'response': '⚠️ Gmail is not connected yet. Please go to the **Gmail tab** and connect with your email + app password first, then come back here.\n\nSteps:\n1. Click "📧 Gmail" tab above\n2. Enter your Gmail address\n3. Enter your 16-character app password from https://myaccount.google.com/apppasswords\n4. Click "Connect Gmail"\n5. Come back to chat'})
    
    # Auto-fetch emails if user is asking about them and we don't have any
    global cached_emails
    if is_email_related and gmail_creds.get('email') and (not cached_emails or len(cached_emails) == 0):
        print("Auto-fetching emails for chat context...", flush=True)
        cached_emails = fetch_emails_imap(gmail_creds['email'], gmail_creds['password'])
    
    # === HANDLE CONFIRMATIONS for pending actions ===
    if conversation_state.get('pending_action'):
        action = conversation_state['pending_action']
        data = conversation_state['pending_data']
        
        # User confirms sending
        if action == 'send_email' and any(w in msg_lower for w in ['yes', 'send', 'send it', 'confirm', 'go ahead', 'ok', 'okay', 'sure']):
            result = send_email_smtp(data['to'], data['subject'], data['body'], data.get('message_id'))
            conversation_state['pending_action'] = None
            conversation_state['pending_data'] = {}
            
            if result['success']:
                return jsonify({'response': f"✅ Email sent successfully to {data['to']}!"})
            else:
                return jsonify({'response': f"❌ Failed to send: {result['error']}"})
        
        # User cancels
        if action == 'send_email' and any(w in msg_lower for w in ['no', 'cancel', 'stop', 'don\'t', 'dont', 'nope']):
            conversation_state['pending_action'] = None
            conversation_state['pending_data'] = {}
            return jsonify({'response': "OK, I won't send that email. What else can I help with?"})
        
        # User wants to edit the draft
        if action == 'send_email' and any(w in msg_lower for w in ['edit', 'change', 'modify', 'rewrite', 'different']):
            try:
                response = openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "Rewrite this email reply based on user's new instructions. Output only the new reply body."},
                        {"role": "user", "content": f"Original draft:\n{data['body']}\n\nNew instruction: {msg}"}
                    ],
                    max_tokens=400
                )
                new_body = response.choices[0].message.content
                conversation_state['pending_data']['body'] = new_body
                return jsonify({'response': f"Here's the updated reply:\n\nTo: {data['to']}\nSubject: {data['subject']}\n\n{new_body}\n\n👉 Say 'send' to send it, 'edit' to modify again, or 'cancel' to discard."})
            except Exception as e:
                return jsonify({'response': f'Error: {str(e)[:200]}'})
    
    # === DETECT NEW ACTIONS ===
    
    # Detect "reply to [sender]" or "send reply to [email]"
    reply_keywords = ['reply to', 'send reply', 'respond to', 'write reply', 'draft reply', 'send a reply']
    if any(kw in msg_lower for kw in reply_keywords) or (msg_lower.startswith('send') and 'reply' in msg_lower):
        # Find target email
        target_email = None
        target_index = None
        
        # Check if user mentions a specific sender
        if cached_emails:
            for idx, em in enumerate(cached_emails):
                if em.get('error'):
                    continue
                sender_lower = em.get('sender', '').lower()
                sender_email_lower = em.get('sender_email', '').lower()
                # Match by name or email
                if sender_lower and sender_lower in msg_lower:
                    target_email = em
                    target_index = idx
                    break
                if sender_email_lower and sender_email_lower in msg_lower:
                    target_email = em
                    target_index = idx
                    break
            
            # If no specific match, use most recent email
            if not target_email and cached_emails:
                for em in cached_emails:
                    if not em.get('error'):
                        target_email = em
                        break
        
        if not target_email:
            return jsonify({'response': "I don't see any emails to reply to. Please connect Gmail first and load your emails."})
        
        # Extract instruction (everything that's not the command)
        instruction = msg
        for kw in reply_keywords:
            instruction = instruction.lower().replace(kw, '').strip()
        
        # Generate the reply
        reply_body = generate_ai_reply(target_email, instruction if instruction else "")
        
        # Store pending action
        conversation_state['pending_action'] = 'send_email'
        conversation_state['pending_data'] = {
            'to': target_email['sender_email'],
            'subject': 'Re: ' + target_email['subject'],
            'body': reply_body,
            'message_id': target_email.get('message_id', '')
        }
        
        return jsonify({'response': f"📧 Here's a draft reply to {target_email['sender']}:\n\nTo: {target_email['sender_email']}\nSubject: Re: {target_email['subject']}\n\n{reply_body}\n\n👉 Reply 'send' to send it, 'edit [your changes]' to modify, or 'cancel' to discard."})
    
    # Detect "compose" or "send email to" - new email composition
    compose_keywords = ['compose', 'send email to', 'send an email to', 'send mail to', 'write email to', 'email to']
    if any(kw in msg_lower for kw in compose_keywords):
        try:
            # Use AI to extract recipient and what to write
            extract_response = openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": """Extract email composition details from user message. Respond in JSON format only: {"to": "email@address.com", "subject": "...", "instruction": "what to write about"}.
If user didn't specify subject, generate appropriate one based on instruction.
If email address not found, set "to" to empty string."""},
                    {"role": "user", "content": msg}
                ],
                max_tokens=200
            )
            import json as json_mod
            details_text = extract_response.choices[0].message.content.strip()
            if '{' in details_text:
                json_start = details_text.find('{')
                json_end = details_text.rfind('}') + 1
                details = json_mod.loads(details_text[json_start:json_end])
                
                to_addr = details.get('to', '').strip()
                if not to_addr or '@' not in to_addr:
                    return jsonify({'response': "I need a recipient email address. Please say something like: 'send email to john@example.com about project update'"})
                
                # Generate email body
                body_response = openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "Write a professional email body. Output ONLY the body, no subject line. Be concise and friendly."},
                        {"role": "user", "content": f"Write an email about: {details.get('instruction', '')}"}
                    ],
                    max_tokens=400
                )
                email_body = body_response.choices[0].message.content
                
                # Store pending action
                conversation_state['pending_action'] = 'send_email'
                conversation_state['pending_data'] = {
                    'to': to_addr,
                    'subject': details.get('subject', 'Message from AI Assistant'),
                    'body': email_body
                }
                
                return jsonify({'response': f"📧 Here's the email I'll send:\n\nTo: {to_addr}\nSubject: {details.get('subject', 'Message')}\n\n{email_body}\n\n👉 Reply 'send' to send it, 'edit' to modify, or 'cancel' to discard."})
        except Exception as e:
            print(f"Compose error: {e}", flush=True)
            return jsonify({'response': f'Error composing email: {str(e)[:200]}'})
    
    # Detect schedule meeting
    if any(kw in msg_lower for kw in ['schedule meeting', 'set up meeting', 'book meeting', 'arrange meeting', 'schedule a meeting']):
        try:
            extract_response = openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {"role": "system", "content": "Extract meeting details from user message. Respond in JSON format only: {\"title\": \"...\", \"with\": \"email or name\", \"time\": \"YYYY-MM-DD HH:MM\", \"notes\": \"...\"}. Use empty string for missing fields."},
                    {"role": "user", "content": msg}
                ],
                max_tokens=200
            )
            import json as json_mod
            details_text = extract_response.choices[0].message.content.strip()
            # Try to extract JSON
            if '{' in details_text:
                json_start = details_text.find('{')
                json_end = details_text.rfind('}') + 1
                details = json_mod.loads(details_text[json_start:json_end])
                
                meetings.append({
                    'title': details.get('title', 'Meeting'),
                    'with_': details.get('with', ''),
                    'time': details.get('time', ''),
                    'notes': details.get('notes', '')
                })
                
                with_who = details.get('with', '')
                response_text = f"✅ Meeting scheduled:\n📅 {details.get('title')}\n🕒 {details.get('time')}\n👤 With: {with_who or 'TBD'}\n📝 {details.get('notes', '')}"
                
                # If we have an email, offer to send invite
                if '@' in with_who:
                    response_text += f"\n\nWant me to send an invite email to {with_who}? Reply 'yes' to send."
                    conversation_state['pending_action'] = 'send_email'
                    conversation_state['pending_data'] = {
                        'to': with_who,
                        'subject': f"Meeting Invitation: {details.get('title')}",
                        'body': f"Hi,\n\nI'd like to schedule a meeting with you:\n\n📅 Title: {details.get('title')}\n🕒 Time: {details.get('time')}\n\n{details.get('notes', '')}\n\nPlease confirm if this works for you.\n\nBest regards"
                    }
                
                return jsonify({'response': response_text})
        except Exception as e:
            print(f"Schedule error: {e}", flush=True)
    
    # === REGULAR CHAT with email context ===
    context = ""
    if cached_emails and gmail_creds.get('email'):
        valid_emails = [e for e in cached_emails if not e.get('error')]
        
        # Filter emails based on user query
        relevant_emails = valid_emails
        
        # If user mentions a specific sender, filter
        for em in valid_emails:
            sender_lower = em.get('sender', '').lower()
            sender_email_lower = em.get('sender_email', '').lower()
            # Check for sender mention in query
            for word in msg_lower.split():
                if len(word) > 3 and (word in sender_lower or word in sender_email_lower):
                    relevant_emails = [e for e in valid_emails if word in e.get('sender', '').lower() or word in e.get('sender_email', '').lower()]
                    break
        
        if relevant_emails:
            emails_summary = "\n\n".join([
                f"Email #{idx+1}:\nFrom: {e['sender']} <{e['sender_email']}>\nSubject: {e['subject']}\nPreview: {e.get('preview', '')[:200]}"
                for idx, e in enumerate(relevant_emails[:10])
            ])
            context = f"\n\n=== USER'S EMAILS (you have access to these) ===\n{emails_summary}\n=== END EMAILS ===\n"
    
    try:
        # Build conversation history
        system_prompt = f"""You are an AI email assistant with FULL ACCESS to the user's Gmail account ({gmail_creds.get('email', 'not connected')}).

You CAN:
- Read and analyze the user's emails (provided in context below)
- Summarize emails 
- Suggest how to reply
- When user wants to send a reply, tell them to say "reply to [sender]" or "send reply to [sender]"
- When user wants to schedule, tell them to say "schedule meeting with [email] on [date/time] about [topic]"

You should ALWAYS use the emails provided in the context to answer questions.
Never say "I cannot access your emails" - you HAVE access to them through the context.
{context}"""
        
        messages = [{"role": "system", "content": system_prompt}]
        
        # Add recent history (last 6 messages)
        for h in conversation_state['history'][-6:]:
            messages.append(h)
        
        messages.append({"role": "user", "content": msg})
        
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=messages,
            max_tokens=600
        )
        
        ai_response = response.choices[0].message.content
        
        # Store in history
        conversation_state['history'].append({"role": "user", "content": msg})
        conversation_state['history'].append({"role": "assistant", "content": ai_response})
        # Keep last 20 messages
        if len(conversation_state['history']) > 20:
            conversation_state['history'] = conversation_state['history'][-20:]
        
        return jsonify({'response': ai_response})
    except Exception as e:
        return jsonify({'response': f'Error: {str(e)[:200]}'})

@app.route('/api/gmail-connect', methods=['POST'])
def gmail_connect():
    data = request.json
    email_addr = data.get('email', '').strip()
    password = data.get('password', '').strip().replace(' ', '')
    
    if not email_addr or not password:
        return jsonify({'success': False, 'error': 'Email and password required'})
    
    emails = fetch_emails_imap(email_addr, password)
    
    if emails and len(emails) > 0 and 'error' in emails[0]:
        return jsonify({'success': False, 'error': emails[0]['error']})
    
    gmail_creds['email'] = email_addr
    gmail_creds['password'] = password
    
    global cached_emails
    cached_emails = emails
    
    return jsonify({'success': True, 'emails': emails})

@app.route('/api/emails')
def api_emails():
    if not gmail_creds.get('email'):
        return jsonify({'emails': []})
    
    emails = fetch_emails_imap(gmail_creds['email'], gmail_creds['password'])
    global cached_emails
    cached_emails = emails
    return jsonify({'emails': emails})

@app.route('/api/email/<int:idx>')
def get_email(idx):
    if 0 <= idx < len(cached_emails):
        return jsonify({'email': cached_emails[idx]})
    return jsonify({'email': None})

@app.route('/api/summarize/<int:idx>')
def summarize_email(idx):
    if not (0 <= idx < len(cached_emails)):
        return jsonify({'summary': 'Email not found'})
    
    email_data = cached_emails[idx]
    
    if not openai_client:
        return jsonify({'summary': 'OpenAI not configured'})
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Summarize this email in 2-3 sentences."},
                {"role": "user", "content": f"From: {email_data['sender']}\nSubject: {email_data['subject']}\n\n{email_data.get('full_body', email_data['preview'])}"}
            ],
            max_tokens=200
        )
        return jsonify({'summary': response.choices[0].message.content})
    except Exception as e:
        return jsonify({'summary': f'Error: {str(e)[:200]}'})

@app.route('/api/generate-reply', methods=['POST'])
def generate_reply():
    email_id = request.json.get('email_id')
    instruction = request.json.get('instruction', '')
    
    if not (0 <= email_id < len(cached_emails)):
        return jsonify({'reply': 'Email not found'})
    
    email_data = cached_emails[email_id]
    reply = generate_ai_reply(email_data, instruction)
    return jsonify({'reply': reply})

@app.route('/api/send-reply', methods=['POST'])
def send_reply():
    data = request.json
    to = data.get('to', '')
    subject = data.get('subject', '')
    body = data.get('body', '')
    message_id = data.get('message_id', '')
    
    if not to or not body:
        return jsonify({'success': False, 'error': 'Missing recipient or body'})
    
    result = send_email_smtp(to, subject, body, message_id)
    return jsonify(result)

@app.route('/api/meetings', methods=['GET', 'POST'])
def api_meetings():
    if request.method == 'POST':
        data = request.json
        meetings.append({
            'title': data.get('title', ''),
            'with_': data.get('with_', ''),
            'time': data.get('time', ''),
            'notes': data.get('notes', '')
        })
    return jsonify({'meetings': meetings})

@app.route('/api/send-meeting-invite', methods=['POST'])
def send_meeting_invite():
    data = request.json
    title = data.get('title', '')
    to = data.get('to', '')
    time = data.get('time', '')
    notes = data.get('notes', '')
    
    if '@' not in to:
        return jsonify({'success': False, 'error': 'Recipient must be an email address'})
    
    body = f"""Hi,

I'd like to schedule a meeting with you:

📅 Title: {title}
🕒 Time: {time}

{f'Notes/Agenda:{chr(10)}{notes}' if notes else ''}

Please let me know if this works for you.

Best regards"""
    
    result = send_email_smtp(to, f"Meeting Invitation: {title}", body)
    return jsonify(result)

@app.route('/api/tasks', methods=['GET', 'POST'])
def api_tasks():
    if request.method == 'POST':
        task = request.json.get('task', '').strip()
        if task:
            tasks.append(task)
    return jsonify({'tasks': tasks})

@app.route('/api/notes', methods=['GET', 'POST'])
def api_notes():
    if request.method == 'POST':
        title = request.json.get('title', '').strip()
        content = request.json.get('content', '').strip()
        if title and content:
            notes.append({'title': title, 'content': content})
    return jsonify({'notes': notes})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 7860))
    print(f"Starting Flask on port {port}...", flush=True)
    app.run(host='0.0.0.0', port=port, debug=False)
