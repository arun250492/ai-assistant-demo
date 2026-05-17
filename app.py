import os
import sys
import imaplib
import email
from email.header import decode_header
from flask import Flask, render_template_string, request, jsonify, session

# Print environment info at startup
print("="*60, flush=True)
print("STARTING AI ASSISTANT", flush=True)
print("="*60, flush=True)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
print(f"OPENAI_API_KEY: {'FOUND (' + str(len(OPENAI_API_KEY)) + ' chars)' if OPENAI_API_KEY else 'MISSING'}", flush=True)
print("="*60, flush=True)

app = Flask(__name__)
app.secret_key = "default-secret-key-12345"

# Initialize OpenAI
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

def fetch_emails_imap(email_addr, password):
    """Fetch emails using IMAP - much simpler than OAuth"""
    import socket
    socket.setdefaulttimeout(15)  # 15 second timeout
    
    print(f"Attempting IMAP connection for: {email_addr}", flush=True)
    
    try:
        print("Connecting to imap.gmail.com:993...", flush=True)
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        print("Connected, attempting login...", flush=True)
        mail.login(email_addr, password)
        print("Login successful, selecting inbox...", flush=True)
        mail.select("inbox")
        print("Inbox selected", flush=True)
        
        # Get latest 10 emails
        status, messages = mail.search(None, "ALL")
        if status != "OK":
            return []
        
        email_ids = messages[0].split()
        email_ids = email_ids[-10:]  # Last 10 emails
        email_ids.reverse()  # Newest first
        
        emails_list = []
        for email_id in email_ids:
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
                        sender_name = sender.split('<')[0].strip().strip('"') if '<' in sender else sender
                        
                        # Get email body
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
                            'sender': sender_name[:50],
                            'subject': subject[:80],
                            'preview': body[:200].strip() if body else subject[:200]
                        })
            except Exception as e:
                print(f"Email parse error: {e}", flush=True)
                continue
        
        mail.logout()
        return emails_list
    except imaplib.IMAP4.error as e:
        error_msg = str(e)
        print(f"IMAP authentication error: {error_msg}", flush=True)
        if 'Invalid credentials' in error_msg or 'AUTHENTICATIONFAILED' in error_msg:
            return [{'error': 'Invalid app password. Make sure you copied the 16-character password correctly from Google.'}]
        return [{'error': f'IMAP error: {error_msg}'}]
    except socket.timeout:
        print("Connection timeout", flush=True)
        return [{'error': 'Connection timeout. Hugging Face may be blocking IMAP. Please try again or check logs.'}]
    except socket.gaierror as e:
        print(f"DNS error: {e}", flush=True)
        return [{'error': f'Cannot resolve imap.gmail.com - Hugging Face may be blocking the connection: {str(e)}'}]
    except Exception as e:
        print(f"IMAP error: {type(e).__name__}: {e}", flush=True)
        return [{'error': f'{type(e).__name__}: {str(e)}'}]

HTML = '''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Assistant + Gmail</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: linear-gradient(135deg, #667eea, #764ba2); color: #fff; font-family: Arial, sans-serif; min-height: 100vh; padding: 20px; }
.container { max-width: 1000px; margin: 0 auto; background: #1a1a2e; border-radius: 10px; overflow: hidden; box-shadow: 0 10px 40px rgba(0,0,0,0.3); }
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
.msg-ai { background: #333; margin-right: 60px; }
.input-box { display: flex; gap: 10px; }
input, textarea { flex: 1; padding: 12px; background: #0f0f1e; border: 1px solid #333; color: #fff; border-radius: 5px; font-family: inherit; font-size: 14px; }
input:focus, textarea:focus { outline: none; border-color: #667eea; }
button { padding: 12px 24px; background: #667eea; color: #fff; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; }
button:hover { background: #764ba2; }
.email-item { background: #0f0f1e; padding: 15px; margin: 10px 0; border-left: 3px solid #667eea; border-radius: 5px; }
.email-from { color: #667eea; font-weight: bold; }
.email-subject { margin: 8px 0; font-weight: bold; }
.email-preview { color: #999; font-size: 13px; line-height: 1.5; }
.task-item, .note-item { background: #0f0f1e; padding: 12px; margin: 10px 0; border-left: 3px solid #667eea; border-radius: 5px; }
.note-item h4 { color: #667eea; margin-bottom: 5px; }
h2 { color: #667eea; margin: 15px 0; }
.loading { color: #999; font-style: italic; }
.setup-box { background: #252540; padding: 20px; border-radius: 5px; margin: 15px 0; border: 1px solid #667eea; }
.setup-box ol { margin-left: 20px; line-height: 1.8; }
.setup-box a { color: #667eea; }
.summary-box { background: #252540; border: 1px solid #667eea; padding: 15px; margin: 15px 0; border-radius: 5px; }
.gmail-form { background: #0f0f1e; padding: 20px; border-radius: 5px; margin-bottom: 20px; }
.gmail-form input { margin: 8px 0; }
.gmail-form button { width: 100%; margin-top: 10px; }
</style>
</head>
<body>
<div class="container">
    <header>
        <h1>🤖 AI Assistant + Gmail</h1>
        <div id="status"></div>
    </header>
    
    <div class="tabs">
        <button class="tab active" data-page="chat">💬 Chat</button>
        <button class="tab" data-page="gmail">📧 Gmail</button>
        <button class="tab" data-page="summary">📊 Summary</button>
        <button class="tab" data-page="tasks">✅ Tasks</button>
        <button class="tab" data-page="notes">📝 Notes</button>
        <button class="tab" data-page="setup">⚙️ Setup</button>
    </div>
    
    <div class="content">
        <div id="chat" class="page active">
            <h2>Chat with AI</h2>
            <div class="chatbox" id="chatbox"></div>
            <div class="input-box">
                <input type="text" id="chat-input" placeholder="Ask anything...">
                <button id="send-btn">Send</button>
            </div>
        </div>
        
        <div id="gmail" class="page">
            <h2>Connect Your Gmail</h2>
            <div class="setup-box">
                <strong>📱 Setup Steps:</strong>
                <ol>
                    <li>Enable 2-Factor Authentication: <a href="https://myaccount.google.com/security" target="_blank">myaccount.google.com/security</a></li>
                    <li>Generate App Password: <a href="https://myaccount.google.com/apppasswords" target="_blank">myaccount.google.com/apppasswords</a></li>
                    <li>Select "Mail" → "Other" → Type "AI Assistant"</li>
                    <li>Copy the 16-character password</li>
                    <li>Enter below and click Connect</li>
                </ol>
            </div>
            <div class="gmail-form">
                <input type="email" id="gmail-email" placeholder="your-email@gmail.com">
                <input type="password" id="gmail-password" placeholder="16-character app password">
                <button id="connect-btn">Connect Gmail</button>
            </div>
            <div id="emails"></div>
        </div>
        
        <div id="summary" class="page">
            <h2>AI Email Summary</h2>
            <button id="summary-btn" style="margin-bottom: 15px;">Get AI Summary of Recent Emails</button>
            <div id="summary-content"></div>
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
        
        <div id="setup" class="page">
            <h2>Setup Guide</h2>
            <div class="setup-box">
                <h3>OpenAI API Key</h3>
                <ol>
                    <li>Visit: <a href="https://platform.openai.com/api/keys" target="_blank">platform.openai.com/api/keys</a></li>
                    <li>Create new secret key</li>
                    <li>Add to Hugging Face Secrets as <strong>OPENAI_API_KEY</strong></li>
                    <li>Restart Space</li>
                </ol>
            </div>
            <div class="setup-box">
                <h3>Gmail Setup (Easy - No OAuth!)</h3>
                <ol>
                    <li>Enable 2FA on your Google account</li>
                    <li>Go to <a href="https://myaccount.google.com/apppasswords" target="_blank">myaccount.google.com/apppasswords</a></li>
                    <li>Generate app password for "Mail"</li>
                    <li>Use that password in Gmail tab (not your regular password)</li>
                    <li>Done! No complex OAuth needed.</li>
                </ol>
            </div>
        </div>
    </div>
</div>

<script>
document.querySelectorAll('.tab').forEach(function(tab) {
    tab.addEventListener('click', function() {
        var page = this.getAttribute('data-page');
        document.querySelectorAll('.page').forEach(function(p) { p.classList.remove('active'); });
        document.querySelectorAll('.tab').forEach(function(t) { t.classList.remove('active'); });
        document.getElementById(page).classList.add('active');
        this.classList.add('active');
        
        if (page === 'tasks') loadTasks();
        if (page === 'notes') loadNotes();
    });
});

function checkStatus() {
    fetch('/api/status').then(function(r) { return r.json(); }).then(function(d) {
        var s = document.getElementById('status');
        s.innerHTML = '';
        if (d.openai) {
            s.innerHTML += '<span class="status ok">✅ OpenAI Ready</span> ';
        } else {
            s.innerHTML += '<span class="status error">❌ Add OPENAI_API_KEY</span> ';
        }
        if (d.gmail) {
            s.innerHTML += '<span class="status ok">✅ Gmail Connected</span>';
        } else {
            s.innerHTML += '<span class="status error">❌ Gmail Not Connected</span>';
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
    emailsDiv.innerHTML = '<p class="loading">Connecting to Gmail...</p>';
    
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
            emailsDiv.innerHTML = '<p style="color: #ff6b6b;">Error: ' + d.error + '</p>';
        }
    });
});

function displayEmails(emails) {
    var div = document.getElementById('emails');
    if (!emails || emails.length === 0) {
        div.innerHTML = '<p class="loading">No emails found</p>';
        return;
    }
    if (emails[0].error) {
        div.innerHTML = '<p style="color: #ff6b6b;">Error: ' + emails[0].error + '</p>';
        return;
    }
    div.innerHTML = '<h3 style="margin-bottom: 10px; color: #4ade80;">✅ Connected! Latest emails:</h3>' + 
        emails.map(function(e) {
            return '<div class="email-item">' +
                '<div class="email-from">' + e.sender + '</div>' +
                '<div class="email-subject">' + e.subject + '</div>' +
                '<div class="email-preview">' + e.preview + '</div>' +
                '</div>';
        }).join('');
}

document.getElementById('summary-btn').addEventListener('click', function() {
    var c = document.getElementById('summary-content');
    c.innerHTML = '<p class="loading">Getting AI summary...</p>';
    fetch('/api/summary').then(function(r) { return r.json(); }).then(function(d) {
        c.innerHTML = '<div class="summary-box"><h3>Summary</h3><p>' + d.summary + '</p></div>';
    });
});

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
setInterval(checkStatus, 5000);
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
        'gmail': bool(gmail_creds.get('email'))
    })

@app.route('/api/chat', methods=['POST'])
def api_chat():
    msg = request.json.get('message', '')
    
    if not openai_client:
        return jsonify({'response': 'OpenAI not configured. Add OPENAI_API_KEY to secrets and restart.'})
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful AI assistant."},
                {"role": "user", "content": msg}
            ],
            max_tokens=500
        )
        return jsonify({'response': response.choices[0].message.content})
    except Exception as e:
        return jsonify({'response': f'Error: {str(e)[:200]}'})

@app.route('/api/gmail-connect', methods=['POST'])
def gmail_connect():
    data = request.json
    email_addr = data.get('email', '').strip()
    password = data.get('password', '').strip().replace(' ', '')  # Remove spaces from app password
    
    if not email_addr or not password:
        return jsonify({'success': False, 'error': 'Email and password required'})
    
    emails = fetch_emails_imap(email_addr, password)
    
    if emails and len(emails) > 0 and 'error' in emails[0]:
        return jsonify({'success': False, 'error': emails[0]['error']})
    
    # Save credentials in session
    gmail_creds['email'] = email_addr
    gmail_creds['password'] = password
    
    return jsonify({'success': True, 'emails': emails})

@app.route('/api/summary')
def api_summary():
    if not openai_client:
        return jsonify({'summary': 'OpenAI not configured'})
    
    if not gmail_creds.get('email'):
        return jsonify({'summary': 'Please connect Gmail first in the Gmail tab'})
    
    emails = fetch_emails_imap(gmail_creds['email'], gmail_creds['password'])
    
    if not emails or (emails and 'error' in emails[0]):
        return jsonify({'summary': 'Could not fetch emails'})
    
    email_text = "\n\n".join([
        f"From: {e['sender']}\nSubject: {e['subject']}\nPreview: {e['preview']}"
        for e in emails[:5]
    ])
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are an email assistant. Summarize the user's emails concisely, highlighting what's important."},
                {"role": "user", "content": f"Summarize these recent emails:\n\n{email_text}"}
            ],
            max_tokens=400
        )
        return jsonify({'summary': response.choices[0].message.content})
    except Exception as e:
        return jsonify({'summary': f'Error: {str(e)[:200]}'})

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
