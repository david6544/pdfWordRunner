from pypdf import PdfReader
import tkinter as tk
import argparse
import sys


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "A PDF Parser that displays each word in sequence to improve readability"
        )
    )
    parser.add_argument("--file", "-f", default="example.pdf", help="PDF file to read")
    parser.add_argument("--wpm", type=int, default=300, help="Words per minute (positive integer)")
    parser.add_argument("--font-size", type=int, default=150, help="Base font size (pixels)")
    parser.add_argument("--fullscreen", action="store_true", help="Start in fullscreen mode")
    parser.add_argument("--start-paused", action="store_true", help="Start paused")
    return parser


def load_words_from_pdf(path):
    try:
        reader = PdfReader(path)
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF '{path}': {e}")

    all_text = []
    for p in reader.pages:
        try:
            text = p.extract_text()
        except Exception:
            text = None
        if text:
            all_text.append(text)

    joined = " ".join(all_text)
    words = [w for w in joined.split() if w.strip()]
    return words


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.wpm <= 0:
        print("--wpm must be a positive integer; using 300.")
        args.wpm = 300

    delay_ms = int(60000 / args.wpm)

    try:
        words = load_words_from_pdf(args.file)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    if not words:
        print("No text found in PDF or the file is empty.")
        sys.exit(1)

    root = tk.Tk()
    if args.fullscreen:
        try:
            root.wm_overrideredirect(True)
            root.attributes("-fullscreen", True)
        except Exception:
            pass
    root.attributes("-topmost", True)

    # font size relative to screen height
    screen_h = root.winfo_screenheight()
    safe_font = max(10, min(args.font_size, int(screen_h * 0.28)))

    label = tk.Label(root, text="", font=("Arial", safe_font))
    label.pack(expand=True)

    word_index = 0
    paused = bool(args.start_paused)
    word_id = None

    def update_label_for_index():
        nonlocal word_index
        if 0 <= word_index < len(words):
            label.config(text=words[word_index])
        else:
            label.config(text="")


    def display_next_word():
        nonlocal word_index, word_id, paused
        if paused:
            word_id = None
            return

        if word_index < len(words):
            label.config(text=words[word_index])
            word_index += 1
            word_id = root.after(delay_ms, display_next_word)
        else:
            word_id = None


    def pause_play(event=None):
        nonlocal paused, word_id
        paused = not paused
        if paused:
            if word_id is not None:
                try:
                    root.after_cancel(word_id)
                except Exception:
                    pass
                word_id = None
            # Visual cue for paused state
            label.config(fg="gray")
        else:
            label.config(fg="black")
            display_next_word()


    def move_left(event=None):
        nonlocal paused, word_index
        if not paused:
            return
        word_index = max(word_index - 1, 0)
        update_label_for_index()


    def move_right(event=None):
        nonlocal paused, word_index
        if not paused:
            return
        word_index = min(word_index + 1, len(words) - 1)
        update_label_for_index()


    # Bind keys globally so focus won't block behavior
    root.bind_all("<space>", pause_play)
    root.bind_all("<Left>", move_left)
    root.bind_all("<Right>", move_right)
    root.bind_all("<Escape>", lambda e: root.destroy())

    root.focus_set()

    # Start
    if not paused:
        display_next_word()
    else:
        update_label_for_index()

    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        raise

    
    
    

    
    