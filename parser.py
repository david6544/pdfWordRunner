import argparse
import sys
import tkinter as tk

# Use pdfplumber for more robust text + layout extraction
import pdfplumber
import threading
from collections import OrderedDict
from PIL import Image, ImageTk


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "A PDF Parser that displays each word using RSVP to improve wpm read"
        )
    )
    parser.add_argument("--file", "-f", required=True, help="PDF file to read")
    parser.add_argument("--wpm", type=int, default=450, help="Words per minute (positive integer)")
    parser.add_argument("--font-size", type=int, default=60, help="Base font size (pixels)")
    parser.add_argument("--fullscreen", action="store_true", help="Start in fullscreen mode")
    parser.add_argument("--start-paused", action="store_true", help="Start paused")
    parser.add_argument("--resolution", type=int, default=None, help="Render resolution (DPI) for PDF pages; higher gives crisper images")
    parser.add_argument("--no-fit", dest="fit", action="store_false", help="Do not scale pages to fit the window; show at full rendered size with scrollbars")
    parser.add_argument("--cache-size", type=int, default=16, help="Number of rendered pages to keep in memory (LRU)")
    parser.add_argument("--start-page", type=int, default=1, help="Starting page of PDF to process (1-indexed)")
    parser.add_argument("--end-page", type=int, default=-1, help="Ending page of PDF to process (1-indexed). Use -1 for last page")
    
    return parser


def load_pdf_index(path, start_idx, end_idx):
    """
    Load minimal PDF index: return words (with page refs) and open PDF object.
    Caller is responsible for closing the returned pdf when done.
    """
    words = []
    try:
        pdf = pdfplumber.open(path)
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF '{path}': {e}")

    total_pages = len(pdf.pages)
    
    # normalize end_idx: -1 or values >= total_pages -> last page
    if end_idx is None or end_idx < 0 or end_idx >= total_pages:
        end_idx = total_pages - 1
    #clamp
    if start_idx is None or start_idx < 0:
        start_idx = 0
    if start_idx >= total_pages:
        raise RuntimeError(f"start_page {start_idx} is out of range (0..{total_pages-1})")
    if start_idx > end_idx:
        raise RuntimeError(f"start_page ({start_idx}) is after end_page ({end_idx})")

    for i in range(start_idx, end_idx + 1):
        page = pdf.pages[i]
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

    return words, pdf


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

    # Normalize start/end page args (user-facing are 1-indexed)
    # Convert them to 0-based indices for internal use.
    raw_start = getattr(args, 'start_page', 1)
    raw_end = getattr(args, 'end_page', -1)
    try:
        if raw_start is None:
            raw_start = 1
        raw_start = int(raw_start)
    except Exception:
        raw_start = 1
    try:
        raw_end = int(raw_end)
    except Exception:
        raw_end = -1

    if raw_start < 1:
        raw_start = 1
    start_idx = raw_start - 1
    # end: -1 means last page; otherwise convert to 0-based
    end_idx = -1 if raw_end < 1 else (raw_end - 1)

    #compute a DPI so page width maps to screen width or use user provided res
    try:
        # Peek at a page's width (prefer start_idx if provided) in PDF points to compute appropriate DPI
        pdf_tmp = pdfplumber.open(args.file)
        preferred_idx = start_idx if 'start_idx' in locals() and start_idx is not None else 0
        if preferred_idx < 0:
            preferred_idx = 0
        first_page = pdf_tmp.pages[preferred_idx] if pdf_tmp.pages and preferred_idx < len(pdf_tmp.pages) else (pdf_tmp.pages[0] if pdf_tmp.pages else None)
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
        words, pdf = load_pdf_index(args.file, start_idx, end_idx)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    # validate cache size
    if args.cache_size is None or args.cache_size < 1:
        args.cache_size = 8

    # pages cache: render pages on demand into this OrderedDict {page_no: PIL.Image}
    # we keep it as an LRU (oldest evicted when size > args.cache_size)
    pages_cache = OrderedDict()
    pages_lock = threading.Lock()

    def cache_put(pno, pil):
        """Insert page image into cache and evict oldest entries when over capacity."""
        with pages_lock:
            if pno in pages_cache:
                # move to end to mark as recently used
                try:
                    pages_cache.move_to_end(pno)
                except Exception:
                    pass
                return
            pages_cache[pno] = pil
            # evict oldest while exceeding capacity
            try:
                while len(pages_cache) > args.cache_size:
                    pages_cache.popitem(last=False)
            except Exception:
                pass

    def prefetch_pages(start_page, n=2):
        """Background prefetch of next n pages starting after start_page."""
        def _worker():
            for pno in range(start_page + 1, min(start_page + 1 + n, len(pdf.pages))):
                with pages_lock:
                    if pno in pages_cache:
                        # already cached
                        continue
                try:
                    pil = pdf.pages[pno].to_image(resolution=render_dpi).original
                    # insert via helper to enforce LRU eviction
                    try:
                        cache_put(pno, pil)
                    except Exception:
                        with pages_lock:
                            pages_cache[pno] = pil
                except Exception:
                    pass

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

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

    # Small HUD overlay on the canvas showing WPM and current page
    hud_frame = tk.Frame(canvas, bg="#000000", bd=0)
    hud_wpm = tk.Label(hud_frame, text=f"{args.wpm} WPM", fg="white", bg="#000000", font=(None, 11))
    hud_page = tk.Label(hud_frame, text="Page -", fg="white", bg="#000000", font=(None, 11))
    hud_wpm.pack(side=tk.LEFT, padx=(6, 8))
    hud_page.pack(side=tk.LEFT, padx=(0, 6))
    # place HUD into the canvas (top-left) and keep its item id so we can raise it later
    hud_window_id = canvas.create_window(10, 10, anchor='nw', window=hud_frame)

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

        r_left = int(left * current_display_scale) + current_offset_x
        r_right = int(right * current_display_scale) + current_offset_x
        r_top = int(top * current_display_scale) + current_offset_y
        r_bottom = int(bottom * current_display_scale) + current_offset_y

        # remove previous rect then draw new one (use a tag so HUD isn't deleted)
        try:
            canvas.delete("highlight")
        except Exception:
            pass
        current_rect = canvas.create_rectangle(r_left, r_top, r_right, r_bottom, outline='red', width=3, tags=("highlight",))

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
                cx = (r_left + r_right) // 2
                cy = (r_top + r_bottom) // 2
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
        if page_no is None or page_no < 0 or page_no >= len(pdf.pages):
            return

        with pages_lock:
            pil = pages_cache.get(page_no)
        if pil is None:
            try:
                pil = pdf.pages[page_no].to_image(resolution=render_dpi).original
            except Exception:
                # fallback blank image
                page_obj = pdf.pages[page_no]
                w_px = int(page_obj.width * render_dpi / 72)
                h_px = int(page_obj.height * render_dpi / 72)
                pil = Image.new("RGB", (max(1, w_px), max(1, h_px)), "white")
            # use cache_put to insert and evict if needed
            try:
                cache_put(page_no, pil)
            except Exception:
                with pages_lock:
                    pages_cache[page_no] = pil

        current_page_no = page_no
        # remove only PDF image and highlight items so HUD (canvas window) remains
        try:
            canvas.delete("pdfimg")
        except Exception:
            pass
        try:
            canvas.delete("highlight")
        except Exception:
            pass
        canvas.update_idletasks()
        img_w, img_h = pil.size
        page_meta = pdf.pages[current_page_no]

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
        try:
            canvas.delete("highlight")
        except Exception:
            pass
        current_rect = None
        # update HUD for current page
        try:
            total = len(pdf.pages)
            if current_page_no is None:
                hud_page.config(text=f"Page - / {total}")
            else:
                hud_page.config(text=f"Page {current_page_no + 1} / {total}")
            # ensure HUD stays on top of canvas image
            try:
                canvas.tag_raise(hud_window_id)
            except Exception:
                pass
        except Exception:
            pass


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

            # remove previous rect then draw new one (use a tag so HUD isn't deleted)
            try:
                canvas.delete("highlight")
            except Exception:
                pass
            current_rect = canvas.create_rectangle(rx0, rtop, rx1, rbottom, outline='red', width=3, tags=("highlight",))

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
        
                # Page controls in the overlay
    def goto_next_page(step: int):
        nonlocal word_index, word_id, paused, current_page_no
        if current_page_no is None:
            next_page = 0 if step >= 0 else max(0, len(pdf.pages) - 1)
        else:
            next_page = max(0, min(current_page_no + step, len(pdf.pages) - 1))
        if next_page == current_page_no:
            return
        # find first word index on that page
        found = None
        for i, w in enumerate(words):
            if w.get("page") == next_page:
                found = i
                break
        if found is None:
            return

        # cancel scheduled advancement
        if word_id is not None:
            try:
                root.after_cancel(word_id)
            except Exception:
                pass

        word_index = found
        render_page(next_page)
        update_label_for_index()

        # prefetch subsequent pages
        try:
            prefetch_pages(next_page)
        except Exception:
            pass

        # resume auto-advance if not paused
        if not paused:
            word_id = root.after(delay_ms, display_next_word)

    btn_frame = tk.Frame(overlay)
    btn = tk.Button(btn_frame, text="Next Page", command=lambda: goto_next_page(1))
    btn2 = tk.Button(btn_frame, text="Prev Page", command=lambda: goto_next_page(-1))
    btn.pack(side=tk.LEFT, padx=6, pady=6)
    btn2.pack(side=tk.LEFT, padx=6, pady=6)
    btn_frame.pack(side=tk.BOTTOM, fill=tk.X)


    # Bind keys globally so focus won't block behavior
    root.bind_all("<space>", pause_play)
    root.bind_all("<Left>", move_left)
    root.bind_all("<Right>", move_right)
    root.bind_all("<Escape>", lambda e: root.destroy())
    root.state('zoomed')
    root.focus_set()

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

    
    
    

    
    