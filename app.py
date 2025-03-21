from flask import Flask, render_template, request, redirect, url_for, session
import fitz  # PyMuPDF
import google.generativeai as genai
from supabase import create_client, Client
import uuid
from dotenv import load_dotenv
import os
from datetime import datetime
from functools import wraps

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Required for session management

# Configure Supabase
SUPABASE_URL = os.getenv("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.getenv("NEXT_PUBLIC_SUPABASE_ANON_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Configure Gemini API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.0-flash')

# Authentication decorator
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Authentication routes
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form['email']
        password = request.form['password']
        
        try:
            user = supabase.auth.sign_up({
                "email": email,
                "password": password
            })
            return redirect(url_for('login'))
        except Exception as e:
            return f"Registration failed: {str(e)}"
    
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form['email']
        password = request.form['password']
        
        try:
            user = supabase.auth.sign_in_with_password({
                "email": email,
                "password": password
            })
            session['user'] = {
                'id': user.user.id,
                'email': user.user.email,
                'token': user.session.access_token
            }
            # Set the access token for authenticated requests
            supabase.postgrest.auth(session['user']['token'])
            return redirect(url_for('index'))
        except Exception as e:
            return f"Login failed: {str(e)}"
    
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

# Existing PDF functionality with authentication
def ensure_table_exists():
    try:
        supabase.table("pdf_text").select("*").limit(1).execute()
        supabase.table("pdf_metadata").select("*").limit(1).execute()
        supabase.table("pdf_chat").select("*").limit(1).execute()
    except Exception as e:
        print(f"Error verifying tables: {str(e)}")
        print("""
        Required tables:
        1. pdf_text (columns: row_id, id, page_number, text, created_at, user_id)
        2. pdf_metadata (columns: id, name, uploaded_at, user_id)
        3. pdf_chat (columns: id, pdf_id, message_type, content, timestamp, user_id)
        """)
        exit(1)

ensure_table_exists()

def store_chat_message(pdf_id, message_type, content):
    supabase.table("pdf_chat").insert({
        "pdf_id": pdf_id,
        "message_type": message_type,
        "content": content,
        "user_id": session['user']['id']
    }).execute()

def get_chat_history(pdf_id):
    response = supabase.table("pdf_chat").select("*").eq("pdf_id", pdf_id).eq(
        "user_id", session['user']['id']
    ).order("timestamp").execute()
    return [{
        'type': item['message_type'],
        'content': item['content'],
        'timestamp': datetime.fromisoformat(item['timestamp']).strftime("%H:%M")
    } for item in response.data]

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    if request.method == "POST":
        if "file" not in request.files:
            return redirect(request.url)
        file = request.files["file"]
        if file.filename == "":
            return redirect(request.url)
        
        if file and file.filename.endswith(".pdf"):
            pdf_id = str(uuid.uuid4())
            pages = extract_text_from_pdf(file)
            
            # Debug: Print the user ID
            print(f"User ID: {session['user']['id']}")
            
            for page_number, text in enumerate(pages):
                supabase.table("pdf_text").insert({
                    "id": pdf_id,
                    "page_number": page_number + 1,
                    "text": text,
                    "user_id": session['user']['id']  # Ensure user_id is included
                }).execute()
            
            supabase.table("pdf_metadata").insert({
                "id": pdf_id,
                "name": file.filename,
                "uploaded_at": datetime.now().isoformat(),
                "user_id": session['user']['id']  # Ensure user_id is included
            }).execute()
            
            session['pdf_id'] = pdf_id
            return redirect(url_for("view_page", page_number=1))
    
    documents = supabase.table("pdf_metadata").select("*").eq(
        "user_id", session['user']['id']
    ).order("uploaded_at", desc=True).execute().data
    return render_template("index.html", documents=documents)

def extract_text_from_pdf(file):
    pdf_document = fitz.open(stream=file.read(), filetype="pdf")
    return [pdf_document.load_page(page_num).get_text() for page_num in range(len(pdf_document))]

@app.route("/view_page/<int:page_number>", methods=["GET", "POST"])
@login_required
def view_page(page_number):
    if 'pdf_id' not in session:
        return redirect(url_for("index"))
    
    pdf_id = session['pdf_id']
    response = supabase.table("pdf_text").select("text").eq("id", pdf_id).eq(
        "user_id", session['user']['id']
    ).eq("page_number", page_number).execute()
    
    if not response.data:
        return redirect(url_for("index"))
    
    text = response.data[0]["text"]
    total_pages = supabase.table("pdf_text").select("page_number", count="exact").eq(
        "id", pdf_id
    ).eq("user_id", session['user']['id']).execute().count
    
    chat_history = get_chat_history(pdf_id)
    
    if request.method == "POST":
        if "next" in request.form and page_number < total_pages:
            page_number += 1
        elif "prev" in request.form and page_number > 1:
            page_number -= 1
        elif "summarize" in request.form:
            context_text = get_context_text(pdf_id, page_number, total_pages)
            summary = summarize_text(context_text)
            store_chat_message(pdf_id, "assistant", summary)
            chat_history.append({'type': 'assistant', 'content': summary, 'timestamp': datetime.now().strftime("%H:%M")})
        elif "question" in request.form:
            question = request.form["question"]
            context_text = get_context_text(pdf_id, page_number, total_pages)
            answer = ask_question(context_text, question, page_number, total_pages)
            store_chat_message(pdf_id, "user", question)
            store_chat_message(pdf_id, "assistant", answer)
            chat_history.append({'type': 'user', 'content': question, 'timestamp': datetime.now().strftime("%H:%M")})
            chat_history.append({'type': 'assistant', 'content': answer, 'timestamp': datetime.now().strftime("%H:%M")})
        elif "chat_message" in request.form:
            question = request.form["chat_message"]
            context_text = get_context_text(pdf_id, page_number, total_pages)
            answer = ask_question(context_text, question, page_number, total_pages)
            store_chat_message(pdf_id, "user", question)
            store_chat_message(pdf_id, "assistant", answer)
            chat_history.append({'type': 'user', 'content': question, 'timestamp': datetime.now().strftime("%H:%M")})
            chat_history.append({'type': 'assistant', 'content': answer, 'timestamp': datetime.now().strftime("%H:%M")})
        
        return redirect(url_for("view_page", page_number=page_number))
    
    return render_template("view_page.html",
                         text=text,
                         chat_history=chat_history,
                         page_number=page_number,
                         total_pages=total_pages)

@app.route("/document/<pdf_id>", methods=["GET"])
@login_required
def load_document(pdf_id):
    session['pdf_id'] = pdf_id
    return redirect(url_for("view_page", page_number=1))

# Rest of your original utility functions
def get_context_text(pdf_id, current_page, total_pages, window_size=5):
    start_page = max(1, current_page - window_size // 2)
    end_page = min(total_pages, current_page + window_size // 2)
    
    try:
        response = supabase.table("pdf_text").select("text, page_number").eq("id", pdf_id)\
                    .eq("user_id", session['user']['id'])\
                    .gte("page_number", start_page).lte("page_number", end_page)\
                    .order("page_number").execute()
        
        if not response.data:
            return "No context data found."
        
        return "\n\n".join([f"Page {item.get('page_number', 'N/A')}:\n{item.get('text', 'No text available')}" for item in response.data])
    except Exception as e:
        print(f"Error fetching context: {e}")
        return "Error fetching context data."

def summarize_text(text):
    try:
        response = model.generate_content(f"Provide a comprehensive summary of this legal text, highlighting key arguments, precedents, and conclusions:\n\n{text}")
        return response.text
    except Exception as e:
        return f"Error generating summary: {str(e)}"

def ask_question(context, question, page_number, total_pages):
    try:
        prompt = f"""Legal Document Context (Pages {max(1, page_number - 2)}-{min(total_pages, page_number + 2)}):
        {context}
        
        Question: {question}
        
        Answer the question based on the legal document context above. If unsure, state that the information is not clear from the provided context."""
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Error generating response: {str(e)}"

if __name__ == "__main__":
    app.run