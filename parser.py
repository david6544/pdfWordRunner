import argparse
import sys
import tkinter as tk

# Use pdfplumber for more robust text + layout extraction
import pdfplumber
from PIL import Image, ImageTk


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "A PDF Parser that displays each word in sequence to improve readability"
        )
    )
    parser.add_argument("--file", "-f", default="example.pdf", help="PDF file to read")
    parser.add_argument("--wpm", type=int, default=450, help="Words per minute (positive integer)")
    parser.add_argument("--font-size", type=int, default=60, help="Base font size (pixels)")
    parser.add_argument("--fullscreen", action="store_true", help="Start in fullscreen mode")
    parser.add_argument("--start-paused", action="store_true", help="Start paused")
    parser.add_argument("--resolution", type=int, default=None, help="Render resolution (DPI) for PDF pages; higher gives crisper images")
    parser.add_argument("--no-fit", dest="fit", action="store_false", help="Do not scale pages to fit the window; show at full rendered size with scrollbars")
    
    return parser


def load_pdf_with_positions(path, resolution=300):
    """
    Load the PDF using pdfplumber and return:
      - words: list of dicts {text, page_no, x0,x1,top,bottom}
      - pages_imgs: list of PIL Images for each page (rendered at given resolution)
      - pages_meta: list of page objects (pdfplumber Page) for coordinate reference
    """
    words = []
    pages_imgs = []
    pages_meta = []

    try:
        pdf = pdfplumber.open(path)
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF '{path}': {e}")

    for i, page in enumerate(pdf.pages):
        # extract word boxes (pdfplumber returns word dicts with text,x0,x1,top,bottom)
        try:
            page_words = page.extract_words()
        except Exception:
            page_words = []

        for w in page_words:
            words.append({
                "text": w.get("text", ""),
                "page": i,
                "left": float(w.get("x0", 0.0)),
                "top": float(w.get("top", 0.0)),
                "right": float(w.get("x1", 0.0)),
                "bottom": float(w.get("bottom", 0.0)),
            })

        # render page to an image for display
        try:
            page_image_obj = page.to_image(resolution=resolution)
            pil_img = page_image_obj.original
        except Exception:
            # fallback to a blank image sized to page dimensions
            w_px = int(page.width * resolution / 72)
            h_px = int(page.height * resolution / 72)
            pil_img = Image.new("RGB", (max(1, w_px), max(1, h_px)), "white")

        pages_imgs.append(pil_img)
        pages_meta.append(page)

    pdf.close()
    return words, pages_imgs, pages_meta


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.wpm <= 0:
        print("--wpm must be a positive integer; using 300.")
        args.wpm = 300

    delay_ms = 60000 // args.wpm

    # Create root early so we can query screen resolution and apply fullscreen before rendering
    root = tk.Tk()
    if args.fullscreen:
        try:
            # Prefer the window-manager's zoomed state which behaves better than forcing overredirect
            root.state('zoomed')
        except Exception:
            try:
                root.attributes("-fullscreen", True)
            except Exception:
                pass
    root.attributes("-topmost", True)

    # Query screen size
    root.update_idletasks()
    screen_w = root.winfo_screenwidth()
    screen_h = root.winfo_screenheight()

    # If user provided an explicit resolution, use it; otherwise compute a DPI so page width maps to screen width
    try:
        # Peek at the first page's width in PDF points to compute appropriate DPI
        pdf_tmp = pdfplumber.open(args.file)
        first_page = pdf_tmp.pages[0] if pdf_tmp.pages else None
        page_width_pts = first_page.width if first_page is not None else None
        pdf_tmp.close()
    except Exception:
        page_width_pts = None

    if args.resolution is not None:
        render_dpi = args.resolution
    elif page_width_pts:
        # DPI so that rendered image width ~= screen width (pixels)
        render_dpi = max(72, int(screen_w * 72 / page_width_pts))
    else:
        render_dpi = 300

    try:
        words, pages_imgs, pages_meta = load_pdf_with_positions(args.file, resolution=render_dpi)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    if not words:
        print("No text found in PDF or the file is empty.")
        sys.exit(1)

    # font size relative to screen height
    screen_h = root.winfo_screenheight()
    safe_font = max(10, min(args.font_size, int(screen_h * 0.28)))

    # Layout: left - PDF Canvas, right - word label
    container = tk.PanedWindow(root, orient=tk.HORIZONTAL)
    container.pack(fill=tk.BOTH, expand=True)

    # Left: canvas for page image and highlight with scrollbars (display at full rendered pixels)
    canvas_frame = tk.Frame(container)
    container.add(canvas_frame, minsize=300)
    v_scroll = tk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
    h_scroll = tk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL)
    canvas = tk.Canvas(canvas_frame, bg='black', yscrollcommand=v_scroll.set, xscrollcommand=h_scroll.set)
    v_scroll.config(command=canvas.yview)
    h_scroll.config(command=canvas.xview)
    v_scroll.pack(side=tk.RIGHT, fill=tk.Y)
    h_scroll.pack(side=tk.BOTTOM, fill=tk.X)
    canvas.pack(fill=tk.BOTH, expand=True)

    # Right: word display as a separate overlay window so it can be moved freely
    overlay = tk.Toplevel(root)
    overlay.title("Reader")
    overlay.geometry("320x180+100+100")
    overlay.attributes("-topmost", True)
    # Make overlay transient and lift it above the main window so it overlays the PDF
    try:
        overlay.transient(root)
        overlay.lift()
    except Exception:
        pass
    # Slightly translucent so PDF beneath can be partially seen
    try:
        overlay.attributes("-alpha", 0.95)
    except Exception:
        pass
    # Make closing the overlay quit the whole app
    overlay.protocol("WM_DELETE_WINDOW", lambda: root.destroy())

    label = tk.Label(overlay, text="", font=("Arial", safe_font), wraplength=300, justify='center')
    label.pack(expand=True, fill=tk.BOTH)

    word_index = 0
    paused = bool(args.start_paused)
    word_id = None

    # State for current rendered page
    current_page_no = None
    current_photo = None
    current_rect = None
    current_display_scale = 1.0
    current_offset_x = 0
    current_offset_y = 0

    def update_label_for_index():
        nonlocal word_index
        if 0 <= word_index < len(words):
            label.config(text=words[word_index]["text"])
            # ensure highlight follows the current index
            highlight_current_word(word_index)
        else:
            label.config(text="")


    def highlight_current_word(idx):
        """Draw highlight for the word at index `idx` and scroll canvas to make it visible."""
        nonlocal current_rect
        if idx is None or idx < 0 or idx >= len(words):
            return
        w = words[idx]
        # ensure page rendered
        if current_page_no != w["page"]:
            render_page(w["page"])

        left = w["left"]
        top = w["top"]
        right = w["right"]
        bottom = w["bottom"]

        rx0 = int(left * current_display_scale) + current_offset_x
        rx1 = int(right * current_display_scale) + current_offset_x
        rtop = int(top * current_display_scale) + current_offset_y
        rbottom = int(bottom * current_display_scale) + current_offset_y

        # remove previous rect then draw new one
        if current_rect is not None:
            try:
                canvas.delete(current_rect)
            except Exception:
                pass
        current_rect = canvas.create_rectangle(rx0, rtop, rx1, rbottom, outline='red', width=3)

        # Scroll canvas to make the rectangle visible (center it when possible)
        try:
            c_w = max(1, canvas.winfo_width())
            c_h = max(1, canvas.winfo_height())
            # scrollregion is (0,0,sw,sh)
            sr = canvas.cget('scrollregion')
            if sr:
                parts = [int(x) for x in sr.split()]
                sw = max(1, parts[2])
                sh = max(1, parts[3])
                # target center
                cx = (rx0 + rx1) // 2
                cy = (rtop + rbottom) // 2
                # compute fractions
                fx = max(0.0, min(1.0, (cx - c_w / 2) / (sw - c_w))) if sw > c_w else 0.0
                fy = max(0.0, min(1.0, (cy - c_h / 2) / (sh - c_h))) if sh > c_h else 0.0
                canvas.xview_moveto(fx)
                canvas.yview_moveto(fy)
        except Exception:
            pass


    def render_page(page_no):
        """Render the given page into the canvas according to fit/full-res settings."""
        nonlocal current_page_no, current_photo, current_display_scale, current_offset_x, current_offset_y, current_rect
        if page_no is None or page_no < 0 or page_no >= len(pages_imgs):
            return
        current_page_no = page_no
        pil = pages_imgs[current_page_no]
        canvas.delete("all")
        canvas.update_idletasks()
        img_w, img_h = pil.size
        page_meta = pages_meta[current_page_no]

        if args.fit:
            c_w = max(1, canvas.winfo_width())
            c_h = max(1, canvas.winfo_height())
            fit_scale = min(c_w / img_w, c_h / img_h)
            new_w = max(1, int(img_w * fit_scale))
            new_h = max(1, int(img_h * fit_scale))
            resized = pil.resize((new_w, new_h), Image.LANCZOS)
            current_photo = ImageTk.PhotoImage(resized)
            canvas.create_image((c_w // 2, c_h // 2), image=current_photo, anchor='center', tags="pdfimg")
            canvas.config(scrollregion=(0, 0, c_w, c_h))
            base_scale = img_w / page_meta.width if page_meta.width else 1.0
            current_display_scale = base_scale * fit_scale
            current_offset_x = (c_w - new_w) // 2
            current_offset_y = (c_h - new_h) // 2
            canvas.image = current_photo
        else:
            current_photo = ImageTk.PhotoImage(pil)
            canvas.create_image(0, 0, image=current_photo, anchor='nw', tags="pdfimg")
            canvas.config(scrollregion=(0, 0, img_w, img_h))
            base_scale = img_w / page_meta.width if page_meta.width else 1.0
            current_display_scale = base_scale
            current_offset_x = 0
            current_offset_y = 0
            canvas.image = current_photo
        # remove any previous rect when new page is rendered
        if current_rect is not None:
            try:
                canvas.delete(current_rect)
            except Exception:
                pass
            current_rect = None


    def display_next_word():
        nonlocal word_index, word_id, paused
        nonlocal current_page_no, current_photo, current_rect
        nonlocal current_display_scale, current_offset_x, current_offset_y
        if paused:
            word_id = None
            return

        if word_index < len(words):
            w = words[word_index]
            label.config(text=w["text"])

            # If page changed, render that page image
            if current_page_no != w["page"]:
                render_page(w["page"])

            # draw highlight rectangle for word (mapped to canvas coords)
            left = w["left"]
            top = w["top"]
            right = w["right"]
            bottom = w["bottom"]

            # Map PDF coords -> displayed image pixels using current_display_scale and offsets
            rx0 = int(left * current_display_scale) + current_offset_x
            rx1 = int(right * current_display_scale) + current_offset_x
            rtop = int(top * current_display_scale) + current_offset_y
            rbottom = int(bottom * current_display_scale) + current_offset_y

            # remove previous rect then draw new one
            if current_rect is not None:
                try:
                    canvas.delete(current_rect)
                except Exception:
                    pass
            current_rect = canvas.create_rectangle(rx0, rtop, rx1, rbottom, outline='red', width=3)

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
    root.state('zoomed')
    root.focus_set()

    # Ensure the paned window sash gives the PDF most of the width
    root.update_idletasks()
    try:
        total_w = container.winfo_width()
        if total_w > 200:
            try:
                container.sash_place(0, int(total_w * 0.75), 0)
            except Exception:
                pass
    except Exception:
        pass

    # Render the initial page now that geometry is available and start the loop
    if words:
        render_page(words[word_index]["page"])

    if not paused:
        root.after(100, display_next_word)
    else:
        update_label_for_index()

    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        raise

    
    
    

    
    