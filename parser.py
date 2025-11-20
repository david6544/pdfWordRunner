from pypdf import PdfReader
import tkinter as tk
import argparse



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A PDF Parser that reads out each word in sequence to improve readability for people with dyslexia")
    parser.add_argument("--wpm", help="The words per minute to display at")
    
    args = parser.parse_args()
    

wpm = 1000 / (int(args.wpm) / 60) if args else 300 

reader = PdfReader("example.pdf")
pages = reader.pages

# Extract all text and split into words
all_text = ""
for page in pages:
    all_text += page.extract_text() + " "
words = all_text.split()
words = [word for word in words if word.strip()]  # Remove empty strings

root = tk.Tk()
root.wm_overrideredirect(True)
root.attributes("-fullscreen", True)
root.bind("<Button 1>", lambda evt: root.destroy())

label = tk.Label(text='', font=("Helvetica", 60))
label.pack(expand=True)

word_index = 0

def display_next_word():
    global word_index
    if word_index < len(words):
        label.config(text=words[word_index])
        word_index += 1
        root.after(100, display_next_word)  # Schedule next word after 100ms

# Start displaying words
display_next_word()

root.mainloop()

    
    
    

    
    