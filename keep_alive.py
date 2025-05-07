from flask import Flask
from threading import Thread
# --- Flask keep-alive ---
app = Flask('')
@app.route('/')
def home():
	return "Himari-chan: I'm ready ^^"

def run():
	app.run(host='0.0.0.0', port=8080)

def keep_alive():
	t = Thread(target=run
						)
	t.start()
