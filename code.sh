unzip ramsys-transportation-improved.zip
cd ramsys-improved
pip install -r requirements.txt
cp .env.example .env   # Add your HERE_API_KEY
python init_db.py
python app.py
# → http://localhost:5000/admin (login: admin / ramsys2026)
