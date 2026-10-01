"""The Mailex window."""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import contacts as _contacts
from . import dnd, theme
from .export import FORMATS, ExportOptions, ExportResult, Exporter, eml_bytes, human_size, local, sanitize
from .filters import ATT_TYPE_LABELS, ATT_TYPES, RowFilter, parse_date_bound, parse_extensions
from .indexer import Hit, Indexer, Resolver, default_db_path
from .message import Folder, Message, MessageRow, PSTFile
from .paths import APP, VERSION, default_save_dir, downloads_dir, load_settings, save_settings
from .sources import FILE_TYPES, MailSource, find_sources, open_found, open_source

_HIDE_FOLDERS = {"IPM_SUBTREE"}  # shown flattened - their children matter, they do not
_OPEN_HINT = "Open a mailbox to begin - PST, OST, MBOX, Thunderbird, Apple Mail, Maildir, EML or MSG."
BODY_CACHE_CHARS = 65536            # of body text kept per message for "search bodies too"
BODY_CACHE_BUDGET = 64_000_000      # total characters (roughly 64-256 MB of RAM) before later bodies are kept short (8 KB)


class App(ttk.Frame):
    def __init__(self, root: tk.Tk, open_paths: Optional[List[str]] = None):
        super().__init__(root)
        self.root = root
        self.settings = load_settings()
        self.dark = bool(self.settings.get("dark", True))
        self.c = theme.apply(root, self.dark)
        self.ui = theme.ui_family(root)
        self.mono = theme.mono_family(root)
        self.q: "queue.Queue" = queue.Queue()
        self.files: List[MailSource] = []
        self.folder_of_item: Dict[str, Folder] = {}
        self.file_of_item: Dict[str, MailSource] = {}
        self.rows_of_item: Dict[str, MessageRow] = {}
        self.current_rows: List[MessageRow] = []
        self.current_folder: Optional[Folder] = None
        self.current_message: Optional[Message] = None
        self.busy_jobs = 0
        self.menus: List[tk.Menu] = []
        self.include_sub = tk.BooleanVar(value=bool(self.settings.get("include_subfolders", False)))
        self.only_mail = tk.BooleanVar(value=bool(self.settings.get("only_mail", False)))
        self.search_var = tk.StringVar()
        self.body_var = tk.BooleanVar(value=False)
        self.from_var = tk.StringVar()
        self.to_var = tk.StringVar()
        self.has_att_var = tk.BooleanVar(value=False)
        self.att_type_var = tk.StringVar(value=ATT_TYPE_LABELS["all"])
        self.contacts_var = tk.BooleanVar(value=False)
        # per-row details the listing does not carry, read in the background:
        # id(row) -> [row, body text or None, [(name, inline, size)] or None]
        self._details: Dict[int, list] = {}
        self._details_chars = 0
        self._details_token = 0
        self._details_busy = False
        self._details_job = (0, False, False)
        self._last_filter_apply = 0.0
        self._drag_folder = dnd.DragFolder()
        self._press_item: Optional[str] = None
        self._stats_win = None
        self._search_job = None
        self._list_token = 0
        self._preview_token = 0
        self._sort = ("date", True)
        self._wheel_rem = ("", 0.0)
        self._inspector = None
        self._next_fid = 0
        self._show_token = 0
        self._fill_token = 0
        self._exporting = False
        self._iid_of_row: Dict[int, str] = {}
        # the content index: bodies and attachments of every indexed mailbox, searched from the same box
        self.index_var = tk.BooleanVar(value=False)
        self.auto_index = tk.BooleanVar(value=bool(self.settings.get("auto_index", False)))
        self._indexer: Optional[Indexer] = None
        self._indexer_error = ""
        self._resolver = Resolver()
        self._indexing = False
        self._cancel_index = threading.Event()
        self._index_token = 0
        self._hit_of_row: Dict[int, Hit] = {}
        self._index_note = ""

        root.title(f"{APP} - open any mailbox, export it any way")
        root.geometry(self.settings.get("geometry") or self._default_geometry(root))
        root.minsize(980, 620)
        self.pack(fill="both", expand=True)
        self._build_menu()
        self._build()
        self._bind_wheel()
        self._recolour()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._pump)
        if open_paths:
            self.after(200, lambda: self.open_paths(open_paths))

    # ------------------------------------------------------------- basics
    @staticmethod
    def _default_geometry(root):
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w = min(1380, max(980, sw - 120))
        h = min(900, max(620, sh - 140))
        return f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}"

    def _save_settings(self):
        try:
            self.settings["sash"] = self.pane.sashpos(0)
            self.settings["vsash"] = self.vpane.sashpos(0)
        except Exception:  # noqa: BLE001
            pass
        self.settings.update(dark=self.dark, geometry=self.root.geometry(),
                             include_subfolders=bool(self.include_sub.get()),
                             only_mail=bool(self.only_mail.get()), auto_index=bool(self.auto_index.get()))
        save_settings(self.settings)

    def _on_close(self):
        if self._exporting or self._indexing:
            what = "An export" if self._exporting else "Indexing"
            if not messagebox.askyesno(APP, f"{what} is still running. Stop it and quit?"):
                return
            if self._exporting:
                self._cancel_export.set()
            self._cancel_index.set()
            # let the worker finish the current message, then go
            self.after(400, self._on_close_after_cancel)
            return
        self._save_settings()
        for f in self.files:
            try:
                f.close()
            except Exception:  # noqa: BLE001
                pass
        self._resolver.close_auto_opened()
        if self._indexer is not None:
            self._indexer.close()
        self._drag_folder.cleanup()
        self.root.destroy()

    def _on_close_after_cancel(self):
        if self._exporting or self._indexing:
            self.after(400, self._on_close_after_cancel)
            return
        self._on_close()

    def _busy_with_export(self) -> bool:
        if self._exporting:
            messagebox.showinfo(APP, "Wait for the export to finish (or cancel it) first.")
            return True
        if self._indexing:
            messagebox.showinfo(APP, "Wait for the indexing to finish (or cancel it) first.")
            return True
        return False

    def status_msg(self, text: str, style: str = "Dim.TLabel"):
        self.status.configure(text=text, style=style)

    # --------------------------------------------------------------- menu
    def _build_menu(self):
        m = tk.Menu(self.root, tearoff=0)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Open mailbox files…", accelerator="Ctrl+O", command=self.open_files)
        f.add_command(label="Open a folder (PSTs, MBOX files, Maildir, EML / MSG files…)…", command=self.open_folder)
        f.add_command(label="Close selected file", command=self.close_selected_file)
        f.add_command(label="Close all", command=self.close_all)
        f.add_separator()
        f.add_command(label="Default save folder…", command=self.choose_save_dir)
        f.add_command(label="Reset save folder to Downloads", command=self.reset_save_dir)
        f.add_separator()
        f.add_command(label="Quit", accelerator="Ctrl+Q", command=self._on_close)
        m.add_cascade(label="File", menu=f)
        e = tk.Menu(m, tearoff=0)
        e.add_command(label="Export selected messages…", accelerator="Ctrl+E", command=lambda: self.export_dialog("selected"))
        e.add_command(label="Export this folder…", command=lambda: self.export_dialog("folder"))
        e.add_command(label="Export everything that is open…", command=lambda: self.export_dialog("all"))
        e.add_separator()
        e.add_command(label="Save attachments of selected messages…", command=self.save_selected_attachments)
        e.add_command(label="Save this message as .eml…", command=self.save_current_eml)
        m.add_cascade(label="Export", menu=e)
        v = tk.Menu(m, tearoff=0)
        v.add_command(label="Toggle light / dark", accelerator="Ctrl+D", command=self.toggle_theme)
        v.add_checkbutton(label="Include subfolders in the list", variable=self.include_sub, command=self._refresh_list)
        v.add_checkbutton(label="Only show e-mail items (hide calendar, contacts, tasks)", variable=self.only_mail,
                          command=self._refresh_list)
        v.add_checkbutton(label="Only show contacts", variable=self.contacts_var, command=self._contacts_toggled)
        v.add_separator()
        v.add_command(label="Statistics…", accelerator="Ctrl+T", command=self.show_stats)
        v.add_command(label="Property inspector for this message", accelerator="Ctrl+I", command=self.show_inspector)
        v.add_command(label="Look for orphaned (deleted) messages in this file", command=self.find_orphans)
        v.add_separator()
        v.add_command(label="Clear all filters", command=self.clear_filters)
        m.add_cascade(label="View", menu=v)
        x = tk.Menu(m, tearoff=0)
        x.add_command(label="Index the open mailboxes now", accelerator="Ctrl+Shift+I", command=self.index_now)
        x.add_command(label="Re-index the open mailboxes from scratch", command=lambda: self.index_now(force=True))
        x.add_checkbutton(label="Index mailboxes automatically when they are opened", variable=self.auto_index,
                          command=self._save_settings)
        x.add_separator()
        x.add_checkbutton(label="Search the index (message text and attachments)", accelerator="Ctrl+Shift+F",
                          variable=self.index_var, command=self._index_toggled)
        x.add_command(label="Search syntax…", command=self.show_index_syntax)
        x.add_separator()
        x.add_command(label="What is indexed…", command=self.show_index_stats)
        x.add_command(label="Take the selected mailbox out of the index", command=self.unindex_selected)
        x.add_command(label="Forget mailboxes whose files are gone", command=self.prune_index)
        x.add_command(label="Index location…", command=self.choose_index_location)
        x.add_command(label="Clear the whole index…", command=self.clear_index)
        m.add_cascade(label="Index", menu=x)
        h = tk.Menu(m, tearoff=0)
        h.add_command(label="How it works", command=self.show_guide)
        h.add_command(label=f"About {APP}", command=self.show_about)
        m.add_cascade(label="Help", menu=h)
        self.menus = [m, f, e, v, x, h]
        self.root.config(menu=m)
        mods = ["Control"] + (["Command"] if sys.platform == "darwin" else [])
        for mod in mods:
            self.root.bind(f"<{mod}-o>", lambda _e: self.open_files())
            self.root.bind(f"<{mod}-q>", lambda _e: self._on_close())
            self.root.bind(f"<{mod}-e>", lambda _e: self.export_dialog("selected"))
            self.root.bind(f"<{mod}-f>", lambda _e: self.search_entry.focus_set())
            self.root.bind(f"<{mod}-i>", lambda _e: self.show_inspector())
            self.root.bind(f"<{mod}-t>", lambda _e: self.show_stats())
            self.root.bind(f"<{mod}-d>", lambda _e: self.toggle_theme())
            self.root.bind(f"<{mod}-Shift-I>", lambda _e: self.index_now())
            self.root.bind(f"<{mod}-Shift-F>", lambda _e: self._toggle_index_mode())
        if sys.platform == "darwin":
            try:
                self.root.createcommand("tk::mac::Quit", self._on_close)
            except tk.TclError:
                pass
        # Ctrl+D is also Tk's "delete character" in text fields - keep it as the theme toggle
        for cls in ("TEntry", "Entry", "Text", "TCombobox", "TSpinbox", "Spinbox"):
            self.root.bind_class(cls, "<Control-d>", lambda _e: "break")

    # -------------------------------------------------------------- build
    def _build(self):
        bar = ttk.Frame(self, padding=(12, 10, 12, 6))
        bar.pack(side="top", fill="x")
        self.bar = bar
        ttk.Button(bar, text="Open files…", style="Accent.TButton", command=self.open_files).pack(side="left")
        ttk.Button(bar, text="Open folder…", command=self.open_folder).pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=12, pady=2)
        ttk.Button(bar, text="Export selected…", command=lambda: self.export_dialog("selected")).pack(side="left")
        ttk.Button(bar, text="Export folder…", command=lambda: self.export_dialog("folder")).pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Export everything…", command=lambda: self.export_dialog("all")).pack(side="left", padx=(6, 0))
        self.theme_btn = ttk.Button(bar, text="Light" if self.dark else "Dark", width=6, command=self.toggle_theme)
        self.theme_btn.pack(side="right")
        ttk.Checkbutton(bar, text="Subfolders", variable=self.include_sub, command=self._refresh_list).pack(side="right", padx=(0, 12))
        ttk.Checkbutton(bar, text="E-mail only", variable=self.only_mail, command=self._mail_only_toggled).pack(side="right", padx=(0, 8))

        # the filter row: everything here narrows the list without re-reading the folder
        flt = ttk.Frame(self, padding=(12, 0, 12, 6))
        flt.pack(side="top", fill="x")
        self.filter_bar = flt
        ttk.Label(flt, text="Search").pack(side="left", padx=(0, 6))
        self.search_entry = ttk.Entry(flt, textvariable=self.search_var, width=26)
        self.search_entry.pack(side="left")
        self.search_entry.bind("<Escape>", lambda _e: self.search_var.set(""))
        self.body_check = ttk.Checkbutton(flt, text="Search bodies too", variable=self.body_var, command=self._schedule_search)
        self.body_check.pack(side="left", padx=(6, 0))
        self.index_check = ttk.Checkbutton(flt, text="Search index", variable=self.index_var, command=self._index_toggled)
        self.index_check.pack(side="left", padx=(6, 14))
        ttk.Label(flt, text="From").pack(side="left", padx=(0, 4))
        self.from_entry = ttk.Entry(flt, textvariable=self.from_var, width=11)
        self.from_entry.pack(side="left")
        ttk.Label(flt, text="To").pack(side="left", padx=(6, 4))
        self.to_entry = ttk.Entry(flt, textvariable=self.to_var, width=11)
        self.to_entry.pack(side="left", padx=(0, 14))
        for ent in (self.from_entry, self.to_entry):
            ent.bind("<Escape>", lambda _e, v=ent: v.delete(0, "end"))
        ttk.Checkbutton(flt, text="Has attachments", variable=self.has_att_var, command=self._schedule_search).pack(side="left")
        self.att_type_box = ttk.Combobox(flt, textvariable=self.att_type_var, state="readonly", width=15,
                                         values=[ATT_TYPE_LABELS[k] for k in ATT_TYPES])
        self.att_type_box.pack(side="left", padx=(6, 14))
        self.att_type_box.bind("<<ComboboxSelected>>", lambda _e: self._att_type_chosen())
        ttk.Checkbutton(flt, text="Contacts", variable=self.contacts_var, command=self._contacts_toggled).pack(side="left")
        self.clear_btn = ttk.Button(flt, text="Clear", width=6, command=self.clear_filters)
        self.clear_btn.pack(side="right")
        for var in (self.search_var, self.from_var, self.to_var):
            var.trace_add("write", lambda *_a: self._schedule_search())

        self.pane = ttk.Panedwindow(self, orient="horizontal")
        self.pane.pack(side="top", fill="both", expand=True, padx=12, pady=(0, 4))

        # left: files and folders
        left = ttk.Frame(self.pane)
        self.tree = ttk.Treeview(left, show="tree", selectmode="browse", style="Side.Treeview")
        tsb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_folder_select)
        self.pane.add(left, weight=1)

        # right: list over preview
        self.vpane = ttk.Panedwindow(self.pane, orient="vertical")
        self.pane.add(self.vpane, weight=4)
        top = ttk.Frame(self.vpane)
        cols = ("date", "from", "subject", "to", "size", "att", "match")
        self._plain_columns = ("date", "from", "subject", "to", "size", "att")
        self._index_columns = ("date", "from", "subject", "match", "size", "att")
        self.list = ttk.Treeview(top, columns=cols, displaycolumns=self._plain_columns, show="headings", selectmode="extended")
        heads = {"date": ("Date", 140, False), "from": ("From", 180, True), "subject": ("Subject", 380, True),
                 "to": ("To", 180, True), "size": ("Size", 70, False), "att": ("Att", 40, False),
                 "match": ("Matched text", 420, True)}
        for c in cols:
            text, width, stretch = heads[c]
            self.list.heading(c, text=text, command=lambda cc=c: self._sort_by(cc))
            self.list.column(c, width=width, minwidth=40, stretch=stretch, anchor="e" if c == "size" else "w")
        lsb = ttk.Scrollbar(top, orient="vertical", command=self.list.yview)
        self.list.configure(yscrollcommand=lsb.set)
        self.list.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        self.list.bind("<<TreeviewSelect>>", self._on_message_select)
        self.list.bind("<Control-a>", self._select_all)
        self.list.bind("<Double-1>", lambda _e: self.save_current_eml())
        self.vpane.add(top, weight=3)
        # drag rows out of the list as .eml / .vcf files (needs tkinterdnd2)
        self.drag_enabled = dnd.enable_drag_out(self.list, self.selected_rows, self._drag_folder,
                                                lambda t: self.status_msg(t, "Good.TLabel"), self._drag_began)
        if self.drag_enabled:
            self.list.bind("<ButtonPress-1>", self._on_list_press)
            self.list.bind("<ButtonRelease-1>", self._on_list_release)

        bottom = ttk.Frame(self.vpane, style="Panel.TFrame")
        self.preview_head = tk.Text(bottom, height=6, wrap="word", relief="flat", padx=10, pady=8)
        self.preview_head.pack(side="top", fill="x")
        atts = ttk.Frame(bottom, style="Panel.TFrame")
        atts.pack(side="top", fill="x")
        self.att_label = ttk.Label(atts, text="", style="PanelDim.TLabel", padding=(10, 0, 6, 0))
        self.att_label.pack(side="left")
        self.att_list = tk.Listbox(atts, height=1, relief="flat", activestyle="none", exportselection=False)
        self.att_list.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.att_list.bind("<Double-1>", lambda _e: self.save_attachment())
        self.att_btn = ttk.Button(atts, text="Save attachment…", command=self.save_attachment, state="disabled")
        self.att_btn.pack(side="right", padx=(0, 8), pady=2)
        body_frame = ttk.Frame(bottom, style="Panel.TFrame")
        body_frame.pack(side="top", fill="both", expand=True)
        self.preview = tk.Text(body_frame, wrap="word", relief="flat", padx=10, pady=8)
        psb = ttk.Scrollbar(body_frame, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=psb.set)
        self.preview.pack(side="left", fill="both", expand=True)
        psb.pack(side="right", fill="y")
        self.vpane.add(bottom, weight=2)

        self.status = ttk.Label(self, text=_OPEN_HINT + " Nothing is uploaded anywhere; everything happens on this computer."
                                           + ("  You can also drop a mailbox file or folder onto this window."
                                              if dnd.available() else ""),
                                style="Dim.TLabel", padding=(16, 2, 16, 8), wraplength=1300)
        self.status.pack(side="bottom", fill="x")
        for text_w in (self.preview_head, self.preview):
            text_w.configure(state="disabled")
        # drop a mailbox anywhere on the window to open it
        self.drop_enabled = dnd.enable_drop_in([self.root, self, self.bar, self.filter_bar, self.tree, self.list,
                                                self.preview, self.preview_head, self.status],
                                               self._dropped, ignore=self._drag_folder.contains)
        self.after(300, self._restore_sashes)

    def _dropped(self, paths: List[str]):
        if self._busy_with_export():
            return
        self.open_paths(paths)

    # -- dragging rows out: keep a multi-selection alive through the press --
    def _on_list_press(self, e):
        item = self.list.identify_row(e.y)
        # Shift or Control (plus Command / Option on a Mac) mean the normal selection
        # rules apply. Windows reports NumLock in the Mod1 bit, so it is not tested there.
        mods = 0x1D if sys.platform == "darwin" else 0x05
        if not item or (e.state & mods):
            self._press_item = None
            return None
        sel = self.list.selection()
        if item in sel and len(sel) > 1:
            # a plain press on one of several selected rows would collapse the
            # selection to that row before the drag could start; hold it until
            # the release says it was just a click
            self._press_item = item
            try:
                self.list.focus_set()   # the class binding we are stopping would have done this
            except tk.TclError:
                pass
            return "break"
        self._press_item = None
        return None

    def _on_list_release(self, e):
        item = self._press_item
        self._press_item = None
        if item and self.list.identify_row(e.y) == item:
            self.list.selection_set(item)
            self.list.focus(item)

    def _drag_began(self):
        self._press_item = None

    def _restore_sashes(self):
        try:
            self.pane.sashpos(0, int(self.settings.get("sash", 300)))
            self.vpane.sashpos(0, int(self.settings.get("vsash", 380)))
        except Exception:  # noqa: BLE001
            pass

    def _recolour(self):
        c = self.c
        for t in (self.preview_head, self.preview):
            t.configure(background=c["panel"], foreground=c["text"], insertbackground=c["accent"],
                        selectbackground=c["sel"], selectforeground=c["text"], highlightthickness=0,
                        font=(self.ui, 10))
        self.preview.configure(font=(self.ui, 10))
        self.preview_head.tag_configure("k", foreground=c["dim"], font=(self.ui, 9, "bold"))
        self.preview_head.tag_configure("subj", font=(self.ui, 12, "bold"))
        self.preview.tag_configure("dim", foreground=c["dim"])
        self.preview.tag_configure("mono", font=(self.mono, 9))
        self.preview.tag_configure("k", foreground=c["dim"], font=(self.mono, 9, "bold"))
        self.att_list.configure(background=c["field"], foreground=c["field_text"], selectbackground=c["accent"],
                                selectforeground=c["accent_text"], highlightthickness=1,
                                highlightbackground=c["field_border"], highlightcolor=c["accent"], font=(self.ui, 9))
        self.list.tag_configure("unread", font=(self.ui, 10, "bold"))
        self.list.tag_configure("dim", foreground=c["dim"])
        # a date entry that does not parse gets a red border
        st = ttk.Style(self.root)
        st.configure("Bad.TEntry", bordercolor=c["bad"], lightcolor=c["bad"], darkcolor=c["bad"])
        st.map("Bad.TEntry", bordercolor=[("focus", c["bad"])], lightcolor=[("focus", c["bad"])], darkcolor=[("focus", c["bad"])])
        for menu in self.menus:
            try:
                menu.configure(bg=c["panel"], fg=c["text"], activebackground=c["sel"],
                               activeforeground=c["text"], borderwidth=0)
            except tk.TclError:
                pass
        if self._inspector is not None:
            try:
                self._inspector.configure(bg=c["bg"])
            except tk.TclError:
                self._inspector = None

    def toggle_theme(self):
        self.dark = not self.dark
        self.c = theme.apply(self.root, self.dark)
        self.theme_btn.configure(text="Light" if self.dark else "Dark")
        self._recolour()

    # -------------------------------------------------------- mouse wheel
    _SCROLLABLE = ("Text", "Treeview", "Listbox", "Canvas")
    _WHEEL_SEQS = ("<MouseWheel>", "<Shift-MouseWheel>", "<Button-4>", "<Button-5>",
                   "<Shift-Button-4>", "<Shift-Button-5>")

    def _bind_wheel(self):
        for cls in self._SCROLLABLE:
            for seq in self._WHEEL_SEQS:
                self.root.bind_class(cls, seq, self._on_wheel)
        for seq in self._WHEEL_SEQS:
            self.root.bind_all(seq, self._on_wheel, add="+")

    def _on_wheel(self, event):
        tkc = self.root.tk
        num = getattr(event, "num", 0)
        if isinstance(num, str):
            num = 0
        if num == 4:
            notches = -1.0
        elif num == 5:
            notches = 1.0
        else:
            delta = getattr(event, "delta", 0) or 0
            if not delta:
                return None
            # Tk 8.6 on macOS reports small units; Tk 9 (and Windows/X11) multiples of 120
            notches = -float(delta) if (sys.platform == "darwin" and abs(delta) < 120) else -delta / 120.0
        horizontal = bool(getattr(event, "state", 0) & 0x0001)
        try:
            path = str(tkc.call("winfo", "containing", event.x_root, event.y_root))
        except tk.TclError:
            path = ""
        if not path:
            path = str(getattr(event.widget, "_w", event.widget))
        try:
            while path and str(tkc.call("winfo", "class", path)) not in self._SCROLLABLE:
                path = str(tkc.call("winfo", "parent", path))
        except tk.TclError:
            return None
        if not path:
            return None
        per_notch = 1.0 if sys.platform == "darwin" else 3.0
        want = notches * per_notch
        if self._wheel_rem[0] == path:
            want += self._wheel_rem[1]
        steps = int(want)
        self._wheel_rem = (path, want - steps)
        if steps:
            try:
                tkc.call(path, "xview" if horizontal else "yview", "scroll", steps, "units")
            except tk.TclError:
                pass
        return "break"

    # ------------------------------------------------------ background
    def _run_bg(self, fn, done, label: str = ""):
        """Run fn() on a thread; done(result_or_exception) runs on the Tk thread."""
        self.busy_jobs += 1
        if label:
            self.status_msg(label)

        def worker():
            try:
                res = fn()
            except BaseException as exc:  # noqa: BLE001
                res = exc
            self.q.put((done, res, True))

        threading.Thread(target=worker, daemon=True).start()

    def post(self, fn, arg=None):
        """Run fn(arg) on the Tk thread (safe to call from any thread)."""
        self.q.put((fn, arg, False))

    def _pump(self):
        try:
            while True:
                fn, res, is_job = self.q.get_nowait()
                if is_job:
                    self.busy_jobs = max(0, self.busy_jobs - 1)
                try:
                    fn(res)
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(80, self._pump)

    # ----------------------------------------------------------- opening
    def open_files(self):
        paths = filedialog.askopenfilenames(title="Open mailbox files", filetypes=FILE_TYPES,
                                            initialdir=self.settings.get("last_open_dir") or downloads_dir())
        if paths:
            self.settings["last_open_dir"] = os.path.dirname(paths[0])
            self.open_paths(list(paths))

    def open_folder(self):
        d = filedialog.askdirectory(title="Open a folder: PST / OST / MBOX files, a Maildir, or EML / MSG files",
                                    initialdir=self.settings.get("last_open_dir") or downloads_dir())
        if not d:
            return
        self.settings["last_open_dir"] = d

        def work():
            return find_sources(d)

        def done(res):
            if isinstance(res, BaseException):
                messagebox.showerror(APP, f"Could not search that folder:\n{res}")
                return
            if not res:
                messagebox.showinfo(APP, "Nothing openable was found in that folder: no PST / OST / MBOX files, "
                                         "no Maildir, and no .eml / .msg / .emlx files.")
                return
            self.open_paths([p for _k, p in res], kinds=[k for k, _p in res])

        self._run_bg(work, done, f"Looking for mailboxes under {d}…")

    def open_paths(self, paths: List[str], kinds: Optional[List[str]] = None):
        already = {os.path.normcase(os.path.abspath(f.path)) for f in self.files}
        todo = [(p, (kinds[i] if kinds else None)) for i, p in enumerate(paths)
                if os.path.normcase(os.path.abspath(p)) not in already]
        if not todo:
            self.status_msg("Those files are already open.")
            return
        # a new file means a new session for the body / attachment cache
        self._details_token += 1
        self._details_busy = False
        self._details.clear()
        self._details_chars = 0

        def work():
            out = []
            for p, kind in todo:
                try:
                    if kind:
                        pst = open_found(kind, p)
                    elif os.path.isdir(p):
                        found = find_sources(p)
                        if len(found) == 1:
                            pst = open_found(*found[0])
                        elif not found:
                            raise ValueError("nothing openable in that folder")
                        else:
                            # several things inside: open each of them
                            for k2, p2 in found:
                                try:
                                    src = open_found(k2, p2)
                                    _count_folders(src)
                                    out.append((p2, src, list(src.root.walk()), None))
                                except Exception as exc:  # noqa: BLE001
                                    out.append((p2, None, [], exc))
                            continue
                    else:
                        pst = open_source(p)
                    # pre-walk the folder tree (and count messages for the non-PST sources)
                    # so the UI can build it without touching the file
                    _count_folders(pst)
                    folders = list(pst.root.walk())
                    out.append((p, pst, folders, None))
                except Exception as exc:  # noqa: BLE001
                    out.append((p, None, [], exc))
            return out

        self._run_bg(work, self._files_opened, f"Opening {len(todo)} file{'s' if len(todo) != 1 else ''}…")

    def _files_opened(self, res):
        if isinstance(res, BaseException):
            messagebox.showerror(APP, f"Could not open the files:\n{res}")
            return
        failed = []
        first_item = None
        for p, pst, folders, exc in res:
            if exc is not None:
                failed.append(f"{os.path.basename(p)}: {exc}")
                continue
            self.files.append(pst)
            item = self._add_file_to_tree(pst)
            first_item = first_item or item
        opened = len(res) - len(failed)
        msg = f"Opened {opened} file{'s' if opened != 1 else ''}."
        if failed:
            msg += "  Could not open: " + "; ".join(failed)
            messagebox.showwarning(APP, "Some files could not be opened:\n\n" + "\n".join(failed))
        self.status_msg(msg, "Good.TLabel" if not failed else "Warn.TLabel")
        new_files = [pst for _p, pst, _f, exc in res if exc is None]
        if new_files and self.auto_index.get() and not self._indexing:
            self.after(300, lambda: self.index_now(files=new_files, quiet=True))
        if first_item is not None:
            # select the Inbox of the first new file, else its first folder with messages
            new_file = self.file_of_item[first_item]
            target = None
            fallback = None
            for iid, folder in self.folder_of_item.items():
                if self.file_of_item.get(iid) is not new_file or not self.tree.exists(iid):
                    continue
                if folder.name.lower() == "inbox" and folder.content_count:
                    target = iid
                    break
                if fallback is None and folder.content_count and (not isinstance(new_file, PSTFile) or (folder.nid & 0x1F) == 0x02):
                    fallback = iid
            target = target or fallback or first_item
            self.tree.selection_set(target)
            self.tree.see(target)

    def _add_file_to_tree(self, pst: MailSource) -> str:
        fid = self._next_fid
        self._next_fid += 1
        label = f"{pst.name}   ({pst.kind_label})"
        root_iid = f"f{fid}"
        self.tree.insert("", "end", iid=root_iid, text=label, open=True)
        self.file_of_item[root_iid] = pst
        self.folder_of_item[root_iid] = pst.root

        def add(folder: Folder, parent_iid: str):
            for sf in folder.subfolders():
                if sf.name.upper() in _HIDE_FOLDERS or sf.name.startswith("Top of "):
                    add(sf, parent_iid)          # flatten the wrapper folders
                    continue
                iid = f"f{fid}n{sf.nid:x}"
                n = sf.content_count
                text = f"{sf.name}  ({n})" if n else sf.name
                if isinstance(pst, PSTFile) and (sf.nid & 0x1F) == 0x03:
                    text += "  [search folder]"
                self.tree.insert(parent_iid, "end", iid=iid, text=text, open=sf.name in ("Root - Mailbox",))
                self.file_of_item[iid] = pst
                self.folder_of_item[iid] = sf
                add(sf, iid)

        add(pst.root, root_iid)
        return root_iid

    def close_selected_file(self):
        if self._busy_with_export():
            return
        sel = self.tree.selection()
        if not sel:
            return
        pst = self.file_of_item.get(sel[0])
        if pst is None:
            return
        self._close_file(pst)

    def _close_file(self, pst: MailSource):
        for iid in [i for i, f in self.file_of_item.items() if f is pst]:
            self.file_of_item.pop(iid, None)
            self.folder_of_item.pop(iid, None)
            if self.tree.exists(iid) and not self.tree.parent(iid):
                self.tree.delete(iid)
        self.files = [f for f in self.files if f is not pst]
        self._resolver.forget(pst.path)
        self._list_token += 1          # stop any listing / fill job for this file
        self._preview_token += 1
        pst.close()
        self.current_folder = None
        self.current_message = None
        self.current_rows = []
        self._show_rows([])
        self._clear_preview()
        self.status_msg(f"Closed {pst.name}.")

    def close_all(self):
        if self._busy_with_export():
            return
        for f in list(self.files):
            self._close_file(f)

    # ------------------------------------------------------------ listing
    def _on_folder_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.current_folder = self.folder_of_item.get(sel[0])
        if self.index_var.get():
            # clicking a folder means "show me this folder": leave the index results
            self.index_var.set(False)
            self._index_mode_changed()
        self._refresh_list()

    def _refresh_list(self):
        if self.index_var.get():
            self._schedule_search()        # the index results stay; the toggles re-run the search
            return
        folder = self.current_folder
        if folder is None:
            return
        self._list_token += 1
        token = self._list_token
        include_sub = bool(self.include_sub.get())
        only_mail = bool(self.only_mail.get())

        def work():
            rows: List[MessageRow] = []
            src = folder.walk() if include_sub else [folder]
            for f in src:
                rows.extend(f.messages())
            if only_mail:
                rows = [r for r in rows if _is_mailish(r.message_class)]
            return rows

        def done(res):
            if token != self._list_token:
                return
            if isinstance(res, BaseException):
                self.status_msg(f"Could not list {folder.path_str}: {res}", "Bad.TLabel")
                return
            self.current_rows = res
            self._show_rows(self._filtered(res))
            n = len(self.current_rows)
            self.status_msg(f"{folder.path_str}: {n} item{'s' if n != 1 else ''}"
                            + (" including subfolders" if include_sub else "") + ".")
            self._fill_senders(res)

        self._run_bg(work, done, f"Listing {folder.path_str}…")

    def _fill_senders(self, rows: List[MessageRow]):
        """OST contents tables carry an unreliable sender column (stale references),
        so the sender comes from the message itself; PSTs only need gaps filled.
        Runs in the background in batches and patches the visible rows as it goes."""
        todo = [r for r in rows if not getattr(r, "_filled", False) and (not r.sender or r.pst.sender_from_message)]
        if not todo:
            return
        self._fill_token += 1
        token = self._fill_token
        list_token = self._list_token

        def work():
            for i in range(0, len(todo), 400):
                if token != self._fill_token or list_token != self._list_token:
                    return
                batch = todo[i:i + 400]
                for r in batch:
                    if token != self._fill_token:
                        return
                    r.fill_from_message(force_sender=r.pst.sender_from_message)
                    r._filled = True
                self.post(self._patch_rows, batch)

        threading.Thread(target=work, daemon=True).start()

    def _patch_rows(self, batch: List[MessageRow]):
        for r in batch:
            iid = self._iid_of_row.get(id(r))
            if iid and self.list.exists(iid):
                self.list.set(iid, "from", r.sender)
                if r.subject:
                    self.list.set(iid, "subject", r.subject)

    # ---------------------------------------------------------- filtering
    def _current_filter(self) -> RowFilter:
        since = parse_date_bound(self.from_var.get())
        until = parse_date_bound(self.to_var.get(), end=True)
        self.from_entry.configure(style="Bad.TEntry" if (self.from_var.get().strip() and since is None) else "TEntry")
        self.to_entry.configure(style="Bad.TEntry" if (self.to_var.get().strip() and until is None) else "TEntry")
        label = self.att_type_var.get()
        kind = next((k for k, v in ATT_TYPE_LABELS.items() if v == label), "all")
        indexed = bool(self.index_var.get())
        # in index mode the words went to the index already; only the other filters narrow the hits
        return RowFilter(text="" if indexed else self.search_var.get(),
                         search_body=bool(self.body_var.get()) and not indexed, since=since, until=until,
                         has_attachments=bool(self.has_att_var.get()), attachment_type=kind,
                         contacts_only=bool(self.contacts_var.get()))

    def _body_of(self, row) -> Optional[str]:
        d = self._details.get(id(row))
        return d[1] if d else None

    def _atts_of(self, row):
        d = self._details.get(id(row))
        if d and d[2] is not None:
            return [(name, inline) for name, inline, _size in d[2]]
        return None

    def _filtered(self, rows: List[MessageRow]) -> List[MessageRow]:
        flt = self._current_filter()
        if not flt.active and not flt.needs_body:
            return rows
        out = flt.apply(rows, body=self._body_of if flt.needs_body else None,
                        atts=self._atts_of if flt.needs_attachment_names else None)
        if flt.needs_body or flt.needs_attachment_names:
            self._fill_details(rows, flt.needs_body, flt.needs_attachment_names)
        return out

    def _schedule_search(self):
        if self._search_job:
            self.after_cancel(self._search_job)
        self._search_job = self.after(250, self._apply_filter_now)

    def _apply_filter_now(self):
        self._search_job = None
        self._last_filter_apply = time.time()
        if self.index_var.get():
            self._index_search()
            return
        self._show_rows(self._filtered(self.current_rows))

    def clear_filters(self):
        for v in (self.search_var, self.from_var, self.to_var):
            v.set("")
        for b in (self.body_var, self.has_att_var, self.contacts_var):
            b.set(False)
        self.att_type_var.set(ATT_TYPE_LABELS["all"])
        self._details_token += 1          # stop a body read that only the filter wanted
        self._details_busy = False
        self._schedule_search()

    def _contacts_toggled(self):
        if self.contacts_var.get() and self.only_mail.get():
            self.only_mail.set(False)      # the two would hide everything between them
            self._refresh_list()
            return
        self._schedule_search()

    def _mail_only_toggled(self):
        if self.only_mail.get() and self.contacts_var.get():
            self.contacts_var.set(False)
        self._refresh_list()

    def _att_type_chosen(self):
        if self.att_type_var.get() != ATT_TYPE_LABELS["all"]:
            self.has_att_var.set(True)
        self._schedule_search()

    # -- reading bodies / attachment names in the background ----------------
    def _fill_details(self, rows: List[MessageRow], need_body: bool, need_atts: bool):
        """Open messages in batches on a worker thread and cache what the filter
        needs (body text capped per message, attachment names). Progress goes to
        the status line; the filter is re-applied every couple of seconds and at
        the end, so matches appear as they are found without the window freezing."""
        todo = []
        for r in rows:
            d = self._details.get(id(r))
            if (need_body and (d is None or d[1] is None)) or (need_atts and (d is None or d[2] is None)):
                todo.append(r)
        if not todo:
            return
        if self._details_busy and self._details_job == (self._list_token, need_body or self._details_job[1],
                                                        need_atts or self._details_job[2]):
            return                      # the running job already covers this
        self._details_token += 1
        token = self._details_token
        list_token = self._list_token
        self._details_busy = True
        self._details_job = (list_token, need_body, need_atts)
        total = len(todo)

        def work():
            done = 0
            for i in range(0, total, 100):
                if token != self._details_token or list_token != self._list_token:
                    self.post(self._details_progress, (token, done, total, "aborted"))
                    return
                for r in todo[i:i + 100]:
                    entry = self._details.setdefault(id(r), [r, None, None])
                    try:
                        m = r.open()
                    except Exception:  # noqa: BLE001 - unreadable: remember that, do not retry every time
                        entry[1] = entry[1] if entry[1] is not None else ""
                        entry[2] = entry[2] if entry[2] is not None else []
                        continue
                    if need_body and entry[1] is None:
                        cap = BODY_CACHE_CHARS if self._details_chars < BODY_CACHE_BUDGET else 8192
                        try:
                            text = m.best_text()[:cap]
                        except Exception:  # noqa: BLE001
                            text = ""
                        entry[1] = text
                        self._details_chars += len(text)
                    if need_atts and entry[2] is None:
                        try:
                            entry[2] = [(a.filename, bool(a.hidden or a.is_inline), a.size or len(a.data or b""))
                                        for a in m.attachments()]
                        except Exception:  # noqa: BLE001
                            entry[2] = []
                done = min(total, i + 100)
                self.post(self._details_progress, (token, done, total, done >= total))

        threading.Thread(target=work, daemon=True).start()

    def _details_progress(self, arg):
        token, done, total, finished = arg
        if token != self._details_token:
            return
        if finished == "aborted":
            # the listing changed under the job; whoever changed it has re-shown the rows
            self._details_busy = False
            return
        what = "bodies" if self.body_var.get() else "attachment lists"
        if finished:
            self._details_busy = False
            self._apply_filter_now()
            return
        self.status_msg(f"Reading {what} {done:,} / {total:,}…  (the list fills in as matches are found)")
        if time.time() - self._last_filter_apply > 2.0:
            self._apply_filter_now()
            self.status_msg(f"Reading {what} {done:,} / {total:,}…  (the list fills in as matches are found)")

    def _sort_by(self, col: str):
        key, desc = self._sort
        desc = not desc if key == col else (col == "date")
        self._sort = (col, desc)
        self._show_rows(self._filtered(self.current_rows))

    def _show_rows(self, rows: List[MessageRow]):
        self._show_token += 1
        self.list.delete(*self.list.get_children(""))
        self.rows_of_item.clear()
        self._iid_of_row.clear()
        col, desc = self._sort
        keyfn = {
            "date": lambda r: (r.date.timestamp() if r.date else 0.0),
            "from": lambda r: r.sender.lower(),
            "subject": lambda r: r.subject.lower(),
            "to": lambda r: r.to.lower(),
            "size": lambda r: r.size,
            "att": lambda r: r.has_attachments,
            "match": lambda r: (self._hit_of_row[id(r)].score if id(r) in self._hit_of_row else 0.0),
        }[col]
        rows = sorted(rows, key=keyfn, reverse=desc)
        self._pending_rows = rows
        self._insert_batch(0, self._show_token)
        n = len(rows)
        flt = self._current_filter()
        if self.index_var.get():
            extra = f"; {flt.describe()}" if flt.active else ""
            self.status_msg(f"{n:,} of {len(self.current_rows):,} index hits shown{extra}.{self._index_note}")
        elif flt.active or flt.needs_body:
            reading = "  (still reading…)" if self._details_busy else ""
            self.status_msg(f"{n:,} of {len(self.current_rows):,} items match: {flt.describe()}.{reading}")

    def _insert_batch(self, start: int, token: int):
        if token != self._show_token:
            return
        rows = self._pending_rows
        end = min(len(rows), start + 1500)
        for i in range(start, end):
            r = rows[i]
            iid = f"m{i}"
            tags = () if r.read else ("unread",)
            hit = self._hit_of_row.get(id(r))
            match = (f"{hit.where}: {hit.snippet}" if hit.snippet else f"in {hit.source_name} / {hit.folder}") if hit else ""
            self.list.insert("", "end", iid=iid, tags=tags, values=(
                local(r.date).strftime("%Y-%m-%d %H:%M") if r.date else "",
                r.sender, r.subject or "(no subject)", r.to, human_size(r.size),
                "yes" if r.has_attachments else "", match))
            self.rows_of_item[iid] = r
            self._iid_of_row[id(r)] = iid
        if end < len(rows):
            self.after_idle(lambda: self._insert_batch(end, token))

    def _select_all(self, _e=None):
        self.list.selection_set(self.list.get_children(""))
        return "break"

    def selected_rows(self) -> List[MessageRow]:
        return [self.rows_of_item[i] for i in self.list.selection() if i in self.rows_of_item]

    def listed_rows(self) -> List[MessageRow]:
        return [self.rows_of_item[i] for i in self.list.get_children("") if i in self.rows_of_item]

    # ------------------------------------------------------------ preview
    def _on_message_select(self, _e=None):
        sel = self.list.selection()
        if len(sel) != 1:
            if len(sel) > 1:
                self._clear_preview()
                self._set_head([("subj", f"{len(sel)} messages selected"), ("", "Use Export selected… to write them out.")])
            return
        row = self.rows_of_item.get(sel[0])
        if row is None:
            return
        self._preview_token += 1
        token = self._preview_token
        self.current_message = None

        def work():
            msg = row.open()
            # touch the expensive bits on the worker thread
            text = msg.best_text()
            atts = [(a.filename, a.size or len(a.data or b""), a.is_embedded_message, bool(a.hidden or a.is_inline))
                    for a in msg.attachments()]
            contact = _contacts.parse_contact(msg) if _contacts.is_contact(msg) else None
            # what the preview read, the filters can reuse
            entry = self._details.setdefault(id(row), [row, None, None])
            if entry[1] is None:
                entry[1] = text[:BODY_CACHE_CHARS]
                self._details_chars += len(entry[1])
            if entry[2] is None:
                entry[2] = [(n, inline, sz) for n, sz, _emb, inline in atts]
            return msg, text, atts, contact

        def done(res):
            if token != self._preview_token:
                return
            if isinstance(res, BaseException):
                self._clear_preview()
                self._set_head([("subj", row.subject or "(no subject)"), ("", f"Could not open this message: {res}")])
                return
            msg, text, atts, contact = res
            self.current_message = msg
            if contact is not None:
                self._render_contact(msg, contact, atts)
            else:
                self._render_preview(msg, text, atts)

        self._run_bg(work, done)

    def _set_head(self, parts: List[Tuple[str, str]]):
        w = self.preview_head
        w.configure(state="normal")
        w.delete("1.0", "end")
        for tag, text in parts:
            w.insert("end", text + "\n", tag or ())
        w.configure(state="disabled")

    def _clear_preview(self):
        self.current_message = None
        self._set_head([])
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.configure(state="disabled")
        self.att_list.delete(0, "end")
        self.att_label.configure(text="")
        self.att_btn.configure(state="disabled")

    def _render_preview(self, msg: Message, text: str, atts):
        w = self.preview_head
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("end", (msg.subject or "(no subject)") + "\n", "subj")
        to = ", ".join(r.formatted() for r in msg.recipients_of("To")) or msg.display_to
        cc = ", ".join(r.formatted() for r in msg.recipients_of("Cc")) or msg.display_cc
        w.insert("end", "From  ", "k"); w.insert("end", msg.sender + "\n")
        w.insert("end", "To  ", "k"); w.insert("end", to + "\n")
        if cc:
            w.insert("end", "Cc  ", "k"); w.insert("end", cc + "\n")
        d = local(msg.date)
        w.insert("end", "Date  ", "k"); w.insert("end", (d.strftime("%a %d %b %Y %H:%M:%S %Z") if d else "unknown") + "    ")
        w.insert("end", "Class  ", "k"); w.insert("end", msg.message_class + "    ")
        w.insert("end", "Size  ", "k"); w.insert("end", human_size(msg.size))
        w.configure(state="disabled")
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        if text.strip():
            self.preview.insert("end", text[:200000])
            if len(text) > 200000:
                self.preview.insert("end", "\n\n[preview truncated]", "dim")
        else:
            self.preview.insert("end", "(this item has no readable body)", "dim")
        if msg.warnings:
            self.preview.insert("end", "\n\n" + "\n".join("Note: " + x for x in msg.warnings), "dim")
        self.preview.configure(state="disabled")
        self._show_attachment_row(atts)

    def _show_attachment_row(self, atts):
        self.att_list.delete(0, "end")
        for name, size, emb, _inline in atts:
            self.att_list.insert("end", f"{name}  ({human_size(size)}){'  [message]' if emb else ''}")
        self.att_list.configure(height=min(4, max(1, len(atts))))
        self.att_label.configure(text=f"Attachments ({len(atts)})" if atts else "No attachments")
        self.att_btn.configure(state="normal" if atts else "disabled")

    def _render_contact(self, msg: Message, c, atts):
        """A contact card: the parsed fields rather than the raw body."""
        w = self.preview_head
        w.configure(state="normal")
        w.delete("1.0", "end")
        w.insert("end", (c.name or "(unnamed contact)") + "\n", "subj")
        line2 = ", ".join(x for x in (c.title, c.company) if x)
        if line2:
            w.insert("end", line2 + "\n")
        if c.email:
            w.insert("end", "E-mail  ", "k"); w.insert("end", c.email + "\n")
        d = local(msg.date)
        w.insert("end", "Modified  ", "k"); w.insert("end", (d.strftime("%a %d %b %Y %H:%M") if d else "unknown") + "    ")
        w.insert("end", "Class  ", "k"); w.insert("end", msg.message_class + "    ")
        if msg.folder is not None:
            w.insert("end", "Folder  ", "k"); w.insert("end", msg.folder.path_str)
        w.configure(state="disabled")
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        fields = _contacts.contact_fields(c)
        width = max((len(k) for k, _v in fields), default=8) + 2
        for k, v in fields:
            self.preview.insert("end", k.ljust(width), "k")
            self.preview.insert("end", v + "\n", "mono")
        if c.notes:
            self.preview.insert("end", "\nNotes\n", "k")
            self.preview.insert("end", c.notes[:200000] + "\n")
        notes = list(c.warnings) + list(msg.warnings)
        if notes:
            self.preview.insert("end", "\n" + "\n".join("Note: " + x for x in notes), "dim")
        self.preview.configure(state="disabled")
        self._show_attachment_row(atts)

    # ------------------------------------------------------ single saves
    def save_attachment(self):
        msg = self.current_message
        if msg is None:
            return
        idx = self.att_list.curselection()
        atts = msg.attachments()
        if not atts:
            return
        i = idx[0] if idx else 0
        if i >= len(atts):
            return
        att = atts[i]
        if att.is_embedded_message:
            sub = att.embedded_message
            if sub is None:
                messagebox.showwarning(APP, "That embedded message could not be read.")
                return
            data = eml_bytes(sub)
            name = sanitize(att.filename if att.filename.lower().endswith(".eml") else (sub.subject or "message") + ".eml")
        else:
            data = att.data
            name = sanitize(att.filename)
            if data is None:
                messagebox.showwarning(APP, "That attachment has no data stored in the file "
                                            "(it may have been a link to a file on the original computer).")
                return
        path = filedialog.asksaveasfilename(title="Save attachment", initialfile=name,
                                            initialdir=default_save_dir(self.settings))
        if not path:
            return
        with open(path, "wb") as fh:
            fh.write(data)
        self.status_msg(f"Saved {os.path.basename(path)} ({human_size(len(data))}).", "Good.TLabel")

    def save_current_eml(self):
        sel = self.selected_rows()
        msg = self.current_message
        if len(sel) == 1 and (msg is None or msg.nid != sel[0].nid or msg.pst is not sel[0].pst):
            try:
                msg = sel[0].open()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(APP, f"Could not open that message:\n{exc}")
                return
        if msg is None:
            return
        from .export import message_basename
        path = filedialog.asksaveasfilename(title="Save message as .eml", defaultextension=".eml",
                                            initialfile=message_basename(msg) + ".eml",
                                            filetypes=[("E-mail message", "*.eml")],
                                            initialdir=default_save_dir(self.settings))
        if not path:
            return
        with open(path, "wb") as fh:
            fh.write(eml_bytes(msg))
        self.status_msg(f"Saved {os.path.basename(path)}.", "Good.TLabel")

    def save_selected_attachments(self):
        rows = self.selected_rows() or self.listed_rows()
        if not rows:
            messagebox.showinfo(APP, "Select some messages first.")
            return
        self.export_dialog("selected", preset_format="attachments")

    # ------------------------------------------------------------ export
    def _rows_for_scope(self, scope: str) -> Tuple[List[MessageRow], str]:
        if scope == "selected":
            rows = self.selected_rows()
            if rows:
                return rows, f"{len(rows)} selected message{'s' if len(rows) != 1 else ''}"
            rows = self.listed_rows()
            return rows, f"all {len(rows)} listed messages (nothing was selected)"
        if scope == "folder":
            if self.current_folder is None:
                return [], "no folder is selected"
            rows = [r for f in self.current_folder.walk() for r in f.messages()]
            return rows, f"{len(rows)} messages in {self.current_folder.path_str} and its subfolders"
        rows = [r for pst in self.files for r in pst.all_message_rows()]
        return rows, f"{len(rows)} messages in {len(self.files)} open file{'s' if len(self.files) != 1 else ''}"

    def export_dialog(self, scope: str, preset_format: Optional[str] = None):
        if not self.files:
            messagebox.showinfo(APP, "Open a mailbox first.")
            return
        if self._busy_with_export():
            return
        fmt = preset_format or self.settings.get("last_format", "eml")
        if scope == "selected":
            rows, desc = self._rows_for_scope(scope)
            if not rows:
                messagebox.showinfo(APP, f"Nothing to export: {desc}.")
                return
            ExportDialog(self, rows, desc, fmt)
            return

        def work():
            return self._rows_for_scope(scope)

        def done(res):
            if isinstance(res, BaseException):
                messagebox.showerror(APP, f"Could not collect the messages:\n{res}")
                return
            rows, desc = res
            if not rows:
                messagebox.showinfo(APP, f"Nothing to export: {desc}.")
                return
            ExportDialog(self, rows, desc, fmt)

        self._run_bg(work, done, "Collecting messages…")

    def run_export(self, rows: List[MessageRow], options: ExportOptions):
        self.settings["last_format"] = options.fmt
        self.settings["save_dir_export"] = options.out_dir
        self._save_settings()
        cancel = threading.Event()
        self._cancel_export = cancel
        self._exporting = True
        prog = ProgressWindow(self, len(rows), cancel)

        def progress(i, n, label):
            self.post(lambda _r: prog.update_progress(i, n, label))

        def work():
            ex = Exporter(options, progress, cancel)
            return ex.run(rows)

        def done(res):
            self._exporting = False
            prog.close()
            if isinstance(res, BaseException):
                messagebox.showerror(APP, f"The export failed:\n{res}")
                self.status_msg(f"Export failed: {res}", "Bad.TLabel")
                return
            self._export_finished(res)

        self._run_bg(work, done, f"Exporting {len(rows)} messages…")

    def _export_finished(self, res: ExportResult):
        style = "Good.TLabel" if not res.failed and not res.cancelled else "Warn.TLabel"
        self.status_msg(f"{res.summary()}  ->  {res.out_dir}", style)
        text = res.summary() + f"\n\nSaved under:\n{res.out_dir}"
        if res.errors:
            text += f"\n\n{len(res.errors)} message(s) could not be exported - see export-log.txt in that folder."
        elif res.warnings:
            text += f"\n\n{len(res.warnings)} note(s) were written to export-log.txt (usually attachments that were links, not files)."
        if messagebox.askyesno(APP, text + "\n\nOpen the folder now?"):
            _open_path(res.out_dir)

    # ----------------------------------------------------------- settings
    def choose_save_dir(self):
        path = filedialog.askdirectory(title="Default save folder", initialdir=default_save_dir(self.settings))
        if not path:
            return
        self.settings["save_dir"] = path
        self._save_settings()
        self.status_msg(f"Exports and saved files will go to {path}.", "Good.TLabel")

    def reset_save_dir(self):
        self.settings.pop("save_dir", None)
        self._save_settings()
        self.status_msg(f"Exports and saved files will go to {downloads_dir()}.", "Good.TLabel")

    # -------------------------------------------------------------- index
    def _get_indexer(self) -> Optional[Indexer]:
        """The index, opened on first use; None (with a message) when it cannot be."""
        if self._indexer is not None:
            return self._indexer
        try:
            self._indexer = Indexer(default_db_path(self.settings))
            self._indexer_error = ""
        except Exception as exc:  # noqa: BLE001
            self._indexer_error = str(exc)
            messagebox.showerror(APP, f"The content index could not be opened:\n{exc}\n\n"
                                      "Use Index > Index location… to put it somewhere else, or Clear the whole index.")
            return None
        return self._indexer

    def _toggle_index_mode(self):
        self.index_var.set(not self.index_var.get())
        self._index_toggled()

    def _index_toggled(self):
        if self.index_var.get():
            ix = self._get_indexer()
            if ix is None:
                self.index_var.set(False)
                return
            try:
                st = ix.stats()
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror(APP, f"The index could not be read: {exc}")
                self.index_var.set(False)
                return
            if not st["messages"]:
                self.index_var.set(False)
                if self.files:
                    if messagebox.askyesno(APP, "Nothing has been indexed yet.\n\nIndex the open mailboxes now? "
                                                "Every message is read once, with its attachments, so this takes a "
                                                "while for a large file - afterwards searches are instant."):
                        self.index_now(then_search=True)
                else:
                    messagebox.showinfo(APP, "Nothing has been indexed yet. Open a mailbox, then use "
                                             "Index > Index the open mailboxes now.")
                return
            self._index_mode_changed()
            self._schedule_search()
            self.search_entry.focus_set()
            return
        # back to the folder view
        self._index_token += 1
        self._index_mode_changed()
        if self.current_folder is not None:
            self._refresh_list()
        else:
            self.current_rows = []
            self._show_rows([])
            self.status_msg("Index search off.")

    def _index_mode_changed(self):
        indexed = bool(self.index_var.get())
        self.list.configure(displaycolumns=self._index_columns if indexed else self._plain_columns)
        self.body_check.configure(state="disabled" if indexed else "normal")
        self._sort = ("match", False) if indexed else ("date", True)
        if not indexed:
            self._hit_of_row.clear()
            self._index_note = ""
            self.search_entry.configure(style="TEntry")

    def _index_search(self):
        """Run the search box against the index; the hits become the listed rows,
        so preview, selection, drag-out and every export work on them unchanged."""
        ix = self._get_indexer()
        if ix is None:
            self.index_var.set(False)
            self._index_mode_changed()
            return
        text = self.search_var.get()
        self._index_token += 1
        token = self._index_token
        self._list_token += 1          # a folder listing in flight must not overwrite the hits
        resolver = self._resolver
        resolver.opened.update({os.path.abspath(f.path): f for f in self.files})

        def work():
            hits = ix.search(text, limit=2000, live=True)
            pairs = resolver.rows_for(hits)
            fresh = list(resolver.auto_opened)
            for src in fresh:
                _count_folders(src)
                resolver.adopt(src)
            st = ix.stats()
            return hits, pairs, fresh, st

        def done(res):
            if token != self._index_token or not self.index_var.get():
                return
            if isinstance(res, BaseException):
                self.status_msg(f"Index search failed: {res}", "Bad.TLabel")
                return
            hits, pairs, fresh, st = res
            for src in fresh:
                if not any(f is src for f in self.files):
                    self.files.append(src)
                    self._add_file_to_tree(src)
            rows = [r for _h, r in pairs]
            self._hit_of_row = {id(r): h for h, r in pairs}
            self.current_rows = rows
            missing = len(hits) - len(pairs)
            note = ""
            if missing:
                bad = sorted({os.path.basename(p) for p in resolver.errors})
                note = f"  {missing} hit{'s' if missing != 1 else ''} came from mailboxes that could not be opened"
                note += (": " + ", ".join(bad[:4])) if bad else " (their messages are no longer in the file)"
            if len(hits) >= 2000:
                note += "  Showing the best 2,000 - add words to narrow it down."
            if not text.strip():
                note += f"  Type to search {st['messages']:,} indexed messages in {st['sources']} mailbox{'es' if st['sources'] != 1 else ''}."
            self._index_note = note
            self._show_rows(self._filtered(rows))

        self._run_bg(work, done)

    def index_now(self, files: Optional[List[MailSource]] = None, force: bool = False, quiet: bool = False,
                  then_search: bool = False):
        targets = files if files is not None else list(self.files)
        if not targets:
            if not quiet:
                messagebox.showinfo(APP, "Open a mailbox first; the index is built from the open files.")
            return
        if self._exporting or self._indexing:
            if not quiet:
                self._busy_with_export()
            return
        ix = self._get_indexer()
        if ix is None:
            return
        cancel = threading.Event()
        self._cancel_index = cancel
        self._indexing = True
        prog = ProgressWindow(self, 0, cancel, verb="Indexing")

        def progress(i, n, label):
            self.post(lambda _r: prog.update_progress(i, n, label))

        def work():
            return ix.index_many(targets, progress=progress, cancel=cancel, force=force)

        def done(res):
            self._indexing = False
            prog.close()
            if isinstance(res, BaseException):
                messagebox.showerror(APP, f"Indexing failed:\n{res}")
                self.status_msg(f"Indexing failed: {res}", "Bad.TLabel")
                return
            added = sum(r.added for r in res)
            removed = sum(r.removed for r in res)
            atts = sum(r.attachments for r in res)
            errors = sum(r.errors for r in res)
            unchanged = sum(1 for r in res if r.unchanged)
            secs = sum(r.seconds for r in res)
            cancelled = any(r.cancelled for r in res)
            bits = [f"{added:,} message{'s' if added != 1 else ''} added ({atts:,} attachments read)"]
            if removed:
                bits.append(f"{removed:,} removed")
            if unchanged:
                bits.append(f"{unchanged} mailbox{'es' if unchanged != 1 else ''} already up to date")
            if errors:
                bits.append(f"{errors} could not be read fully")
            text = "Index " + ("stopped" if cancelled else "updated") + ": " + ", ".join(bits) + f" in {secs:.0f}s."
            self.status_msg(text, "Warn.TLabel" if cancelled or errors else "Good.TLabel")
            if then_search and not cancelled:
                self.index_var.set(True)
                self._index_mode_changed()
                self._schedule_search()
            elif self.index_var.get():
                self._schedule_search()

        names = ", ".join(t.name for t in targets[:3]) + ("…" if len(targets) > 3 else "")
        self._run_bg(work, done, f"Indexing {names}…")

    def show_index_stats(self):
        ix = self._get_indexer()
        if ix is None:
            return
        try:
            st = ix.stats()
            srcs = ix.sources()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(APP, f"The index could not be read: {exc}")
            return
        lines = [f"Index file: {st['path']}  ({human_size(st['bytes'])})",
                 f"{st['sources']} mailbox{'es' if st['sources'] != 1 else ''}, {st['messages']:,} messages, "
                 f"{st['attachments']:,} attachments ({st['messages_with_attachment_text']:,} messages with searchable attachment text)",
                 f"PDF text: {st['pdf_engine']}" + ("" if st['pdf_engine'] != "built-in" else
                                                     "  (install PyMuPDF for scanned-layout and complex PDFs)"), ""]
        open_paths = {os.path.normcase(os.path.abspath(f.path)) for f in self.files}
        for r in srcs:
            flag = "open" if os.path.normcase(os.path.abspath(r["path"])) in open_paths else ("missing" if not os.path.exists(r["path"]) else "")
            state = "" if r["complete"] else "  (partial - indexing was stopped)"
            lines.append(f"• {r['name']}  [{r['kind']}]  {r['messages']:,} messages{state}" + (f"  - {flag}" if flag else ""))
            lines.append(f"    {r['path']}")
        if not srcs:
            lines.append("Nothing has been indexed yet.")
        win = tk.Toplevel(self.root)
        win.title("What is indexed")
        win.geometry("760x480")
        win.configure(bg=self.c["bg"])
        txt = tk.Text(win, wrap="word", padx=16, pady=14, relief="flat", background=self.c["panel"],
                      foreground=self.c["text"], selectbackground=self.c["sel"], selectforeground=self.c["text"],
                      highlightthickness=0, font=(self.ui, 10))
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.insert("end", "\n".join(lines))
        txt.configure(state="disabled")

    def show_index_syntax(self):
        messagebox.showinfo("Search syntax", INDEX_SYNTAX)

    def unindex_selected(self):
        if self._busy_with_export():
            return
        sel = self.tree.selection()
        pst = self.file_of_item.get(sel[0]) if sel else None
        if pst is None:
            messagebox.showinfo(APP, "Select a mailbox in the left pane first.")
            return
        ix = self._get_indexer()
        if ix is None:
            return
        n = ix.remove_source(pst.path)
        self.status_msg(f"{pst.name}: {n:,} messages taken out of the index." if n else f"{pst.name} was not in the index.")
        if self.index_var.get():
            self._schedule_search()

    def prune_index(self):
        if self._busy_with_export():
            return
        ix = self._get_indexer()
        if ix is None:
            return
        gone = ix.prune_missing()
        self.status_msg(f"Forgot {len(gone)} mailbox{'es' if len(gone) != 1 else ''} whose files are gone."
                        if gone else "Every indexed mailbox is still where it was.")
        if self.index_var.get():
            self._schedule_search()

    def clear_index(self):
        if self._busy_with_export():
            return
        ix = self._get_indexer()
        if ix is None:
            # an unreadable index file: offer to delete it outright
            path = default_db_path(self.settings)
            if os.path.exists(path) and messagebox.askyesno(APP, f"Delete the index file?\n{path}"):
                for suffix in ("", "-wal", "-shm"):
                    try:
                        os.remove(path + suffix)
                    except OSError:
                        pass
                self.status_msg("Index file deleted.", "Good.TLabel")
            return
        st = ix.stats()
        if not messagebox.askyesno(APP, f"Clear the whole index?\n\n{st['messages']:,} messages from {st['sources']} "
                                        f"mailbox{'es' if st['sources'] != 1 else ''} will have to be indexed again."):
            return
        ix.clear()
        self._index_mode_changed()
        if self.index_var.get():
            self.index_var.set(False)
            self._index_mode_changed()
            self._show_rows(self._filtered(self.current_rows))
        self.status_msg("Index cleared.", "Good.TLabel")

    def choose_index_location(self):
        if self._busy_with_export():
            return
        current = default_db_path(self.settings)
        path = filedialog.asksaveasfilename(title="Where to keep the content index", initialdir=os.path.dirname(current),
                                            initialfile=os.path.basename(current), defaultextension=".db",
                                            filetypes=[("Mailex index", "*.db"), ("All files", "*.*")],
                                            confirmoverwrite=False)
        if not path:
            return
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(current)):
            return
        if self._indexer is not None:
            self._indexer.close()
            self._indexer = None
        self.settings["index_path"] = path
        self._save_settings()
        if self._get_indexer() is None:
            self.settings.pop("index_path", None)
            self._save_settings()
            return
        exists = os.path.exists(path)
        self.status_msg(f"The index now lives at {path}." + ("" if exists else "  It is empty: use Index > Index the open mailboxes now."),
                        "Good.TLabel")
        if self.index_var.get():
            self._schedule_search()

    # ------------------------------------------------------------- extras
    def show_inspector(self):
        msg = self.current_message
        if msg is None:
            messagebox.showinfo(APP, "Select a single message first.")
            return
        win = tk.Toplevel(self.root)
        win.title(f"Properties - {msg.subject or '(no subject)'}")
        win.geometry("860x560")
        win.configure(bg=self.c["bg"])
        self._inspector = win
        tv = ttk.Treeview(win, columns=("id", "name", "type", "value"), show="headings")
        for c, t, w in (("id", "Tag", 70), ("name", "Name", 200), ("type", "Type", 70), ("value", "Value", 480)):
            tv.heading(c, text=t)
            tv.column(c, width=w, stretch=(c == "value"))
        sb = ttk.Scrollbar(win, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)
        sb.pack(side="right", fill="y", pady=10, padx=(0, 10))
        for pid, name, ptype, val in msg.all_properties():
            if isinstance(val, bytes):
                shown = f"{len(val)} bytes: " + val[:48].hex(" ")
            else:
                shown = str(val)
            shown = shown.replace("\r", " ").replace("\n", " ")
            tv.insert("", "end", values=(f"0x{pid:04X}" if isinstance(pid, int) else "header", name,
                                         f"0x{ptype:04X}" if isinstance(ptype, int) else "", shown[:600]))

        def _gone(_e=None, w=win):
            if self._inspector is w:
                self._inspector = None
        win.bind("<Destroy>", _gone, add="+")

    def find_orphans(self):
        sel = self.tree.selection()
        pst = self.file_of_item.get(sel[0]) if sel else (self.files[-1] if self.files else None)
        if pst is None:
            messagebox.showinfo(APP, "Open a file first.")
            return
        if not isinstance(pst, PSTFile):
            messagebox.showinfo(APP, "The orphan scan looks for messages a PST or OST's index still holds but no "
                                     "folder lists. It does not apply to " + pst.kind_label + " sources.")
            return

        def work():
            from . import mapi
            nids = pst.orphan_messages()
            folder = Folder(pst, 0, "(orphaned messages)", pst.root)
            rows = []
            for nid in nids:
                try:
                    m = pst.message(nid, folder=folder)
                    rows.append(MessageRow(folder, {mapi.PR_LTP_ROW_ID: nid, mapi.PR_SUBJECT: m.subject,
                                                    mapi.PR_SENDER_NAME: m.sender, mapi.PR_MESSAGE_DELIVERY_TIME: m.date,
                                                    mapi.PR_MESSAGE_SIZE: m.size, mapi.PR_HASATTACH: m.has_attachments,
                                                    mapi.PR_MESSAGE_CLASS: m.message_class, mapi.PR_DISPLAY_TO: m.display_to}))
                except Exception:  # noqa: BLE001
                    continue
            folder._rows = rows
            folder._subfolders = []
            folder.content_count = len(rows)
            return folder, rows

        def done(res):
            if isinstance(res, BaseException):
                messagebox.showerror(APP, f"Scan failed: {res}")
                return
            folder, rows = res
            if pst not in self.files:
                return
            if not rows:
                messagebox.showinfo(APP, f"{pst.name}: no orphaned messages were found. Every message node "
                                         "is listed by a folder.")
                return
            iid = f"orphans-{id(folder)}"
            root_iid = next((i for i, f in self.file_of_item.items() if f is pst and not self.tree.parent(i)), "")
            self.tree.insert(root_iid, "end", iid=iid, text=f"(orphaned messages)  ({len(rows)})")
            self.file_of_item[iid] = pst
            self.folder_of_item[iid] = folder
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self.status_msg(f"{pst.name}: {len(rows)} orphaned message(s) found - they are listed under "
                            "'(orphaned messages)' and can be exported like any other.", "Good.TLabel")

        self._run_bg(work, done, f"Scanning {pst.name} for orphaned messages…")

    def show_stats(self):
        if not self.files:
            messagebox.showinfo(APP, "Open a mailbox first.")
            return
        if self._stats_win is not None:
            try:
                self._stats_win.lift()
                self._stats_win.focus_set()
                return
            except tk.TclError:
                self._stats_win = None
        self._stats_win = StatsWindow(self)

    def show_about(self):
        messagebox.showinfo(f"About {APP}",
                            f"{APP} {VERSION}\n\nOpens mailboxes in whatever form they come - Outlook PST and OST "
                            "files, MBOX (including Thunderbird and Apple Mail folders), Maildir, and loose EML, "
                            "EMLX and Outlook MSG files - and exports messages as EML, MBOX, PDF, HTML, plain text "
                            "or just the attachments, one at a time or thousands at once.\n\nThe PST/OST and MSG "
                            "readers were written from Microsoft's published specifications; no Outlook, MAPI or "
                            "compiled library is involved.\n\nEverything runs on this computer. Nothing is uploaded anywhere.")

    def show_guide(self):
        win = tk.Toplevel(self.root)
        win.title("How Mailex works")
        win.geometry("720x560")
        win.configure(bg=self.c["bg"])
        txt = tk.Text(win, wrap="word", padx=18, pady=16, relief="flat", background=self.c["panel"],
                      foreground=self.c["text"], selectbackground=self.c["sel"], selectforeground=self.c["text"],
                      highlightthickness=0, font=(self.ui, 10))
        sb = ttk.Scrollbar(win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        txt.tag_configure("h", font=(self.ui, 12, "bold"), foreground=self.c["accent"], spacing1=10, spacing3=4)
        for head, body in GUIDE:
            txt.insert("end", head + "\n", "h")
            txt.insert("end", body + "\n")
        txt.configure(state="disabled")


GUIDE = [
    ("Opening files", "File > Open picks one or more files: Outlook .pst / .ost, .mbox, single .eml / .emlx / .msg "
                      "messages. File > Open a folder works out what is inside it - PST and OST files, MBOX files "
                      "(a Thunderbird profile's Mail folder with its .sbd sub-folders, or Apple Mail's exported "
                      ".mbox packages), a Maildir (cur/new/tmp), or a tree of loose .eml / .msg / .emlx files whose "
                      "directories become the folders - and opens all of it. That is the bulk route. You can also drop "
                      "a mailbox file or folder onto the window. Files are only ever read; nothing in them is changed. "
                      "An OST still in use by Outlook may refuse to open until Outlook is closed."),
    ("Browsing", "The left pane lists each file's folders with message counts. Click a folder to list its messages; "
                 "tick Subfolders to include everything beneath it. Click a column heading to sort. A single click "
                 "previews the message; the attachments row lets you save any one of them. Contacts preview as a "
                 "card - name, company, e-mail addresses, phones, addresses - rather than as raw text."),
    ("Filtering", "The filter row narrows the list without re-reading the folder. Search matches every word you type "
                  "against subject, sender and recipients; tick 'Search bodies too' and the message text is searched "
                  "as well (messages are read in the background and the list fills in as matches turn up - the status "
                  "line shows progress). From / To take a year (2019), a month (2019-03) or a day (2019-03-15); From "
                  "means the start of that period and To its end, and undated items only show when both are blank. "
                  "'Has attachments' keeps messages with attachments, and the box beside it narrows that to documents, "
                  "images, archives or other files (inline pictures such as signature logos do not count). 'Contacts' "
                  "shows contact items only; 'E-mail only' hides calendar, contact, task and note items. Clear resets "
                  "the lot. Whatever is listed is what 'Export selected' takes when nothing is selected."),
    ("Selecting", "Click, Shift-click and Ctrl-click (Cmd-click on a Mac) select messages in the list; Ctrl+A "
                  "selects everything listed. Export selected… writes those. Export folder… takes the folder in the "
                  "left pane with all of its subfolders, and Export everything… takes every open file."),
    ("Drag and drop", "Drag selected messages out of the list and drop them on the desktop, a Finder or Explorer window, "
                      "or any folder: they arrive as .eml files (contacts as .vcf). Up to 200 messages per drag; for "
                      "more, use Export selected. Dropping a mailbox file or folder onto the window opens it. "
                      "Both need the tkinterdnd2 package, which the build scripts include."),
    ("Formats", "EML is the standard single-message format that Outlook, Apple Mail and Thunderbird open directly; "
                "attachments are embedded. MBOX puts a whole folder in one file for importing into Thunderbird or "
                "Apple Mail. PDF, HTML and plain text are for reading and evidence bundles - attachments are saved "
                "in a folder beside each message. Attachments only extracts just the files, optionally only certain "
                "extensions or kinds and without inline pictures. Contacts (vCard + CSV) writes one .vcf per contact "
                "plus a combined contacts.vcf and a contacts.csv at the top of the export, ready to import into Apple "
                "Contacts, Outlook or Google; in every other format contacts also get their .vcf written alongside. "
                "Every export writes an index.csv and index.json listing what was written, and export-log.txt if "
                "anything went wrong."),
    ("Content index", "Index > Index the open mailboxes now reads every message once - subject, people, the full "
                      "text and the contents of attachments (Word, Excel, PowerPoint, OpenDocument, PDF, RTF, HTML, "
                      "text, saved .eml / .msg messages, ZIP archives, and the readable strings of old .doc / .xls "
                      "files) - into a small SQLite database beside the app. Tick 'Search index' in the filter row "
                      "(Ctrl+Shift+F) and the search box then finds words anywhere in that index, instantly, across every "
                      "indexed mailbox - not just the folder you are looking at. Hits list like any other messages: "
                      "preview, select, drag out or export them. The 'Matched text' column shows where the words were "
                      "found. Mailboxes that are not open are opened for you when a hit comes from them. Indexing is "
                      "incremental: a file that has not changed is skipped, and only new messages are read from one that "
                      "has. Turn on 'Index mailboxes automatically when they are opened' to keep it current. Index > "
                      "Search syntax lists the prefixes (from:, to:, subject:, att:, in:, after:, before:, has:att, "
                      "quotes for phrases, ! to exclude). PDFs are read with the built-in reader; the optional PyMuPDF "
                      "package (installed by the build scripts when available) handles complex layouts better. Scanned "
                      "images are not OCR'd."),
    ("Statistics", "View > Statistics (Ctrl+T) sums up the listed folder or everything open: messages per year and "
                   "month, top senders and sender domains, per-folder counts and sizes, the largest messages, "
                   "read/unread, item kinds, and attachment counts once the attachment lists have been read. Save "
                   "it as one CSV per table or as a single HTML page."),
    ("Folder structure", "By default the mailbox's folder tree is recreated under the export folder, with one folder per "
                         "file when several are exported at once. Untick 'Keep folder structure' to put everything in "
                         "one place. File names are 'date time subject' so they sort chronologically."),
    ("Fidelity", "Messages that arrived as real RFC 822 bytes (MBOX, EML, Maildir) are exported to EML and MBOX "
                 "byte for byte. PST and MSG messages are rebuilt into standard MIME from their MAPI properties, "
                 "keeping the original transport headers where the file kept them."),
    ("What cannot be read", "Attachments that were links to files on the original computer have no content in the "
                            "PST. Password-protected PSTs open fine (the password only guards Outlook's UI), but "
                            "files using Windows EFS-style encryption cannot be read without the original key."),
    ("Where files go", "Exports go to your Downloads folder unless you choose another location in the export "
                       "dialog, or set a default under File > Default save folder."),
]


INDEX_SYNTAX = (
    "Words match anywhere - subject, people, message text and attachment contents.\n"
    "The last word you are typing matches as a prefix.\n\n"
    "   invoice 2023             every word, in any field\n"
    "   \"exact phrase\"           words in that order\n"
    "   from:bob   to:alice      sender / recipients (name or address)\n"
    "   subject:renewal          one field only: subject, body, from, to\n"
    "   att:pdf   att:\"q4 report\"  attachment names and their contents\n"
    "   file:xlsx                attachment names only\n"
    "   in:inbox                 folder path contains\n"
    "   source:work.pst          mailbox name or path contains\n"
    "   has:att                  only messages with attachments\n"
    "   after:2023-06  before:2024   date window (year, year-month or full date)\n"
    "   !spam   -newsletter      must not contain\n\n"
    "The From / To dates, 'Has attachments', attachment kind and 'Contacts' filters still narrow the hits."
)


def _count_folders(src: MailSource):
    """Non-PST sources only know their message counts once the folder is read."""
    if isinstance(src, PSTFile):
        return
    for f in src.root.walk():
        f.messages()


def _is_mailish(mc: str) -> bool:
    mc = (mc or "IPM.Note").upper()
    return mc.startswith("IPM.NOTE") or mc.startswith("REPORT.") or mc == "IPM" or mc.startswith("IPM.POST")


def _open_path(path: str):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            webbrowser.open("file://" + path)
    except Exception:  # noqa: BLE001
        pass


class ExportDialog(tk.Toplevel):
    def __init__(self, app: App, rows: List[MessageRow], desc: str, fmt: str):
        super().__init__(app.root)
        self.app = app
        self.rows = rows
        c = app.c
        self.title("Export messages")
        self.configure(bg=c["bg"])
        self.resizable(False, False)
        self.transient(app.root)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        n_contacts = sum(1 for r in rows if _contacts.is_contact_class(r.message_class))
        title = f"Export {desc}"
        if n_contacts:
            title += f"  ({n_contacts} contact{'s' if n_contacts != 1 else ''} among them)"
        ttk.Label(body, text=title, style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        ttk.Label(body, text="Format", style="Field.TLabel").grid(row=1, column=0, sticky="w", pady=4)
        self.fmt = tk.StringVar(value=fmt if fmt in FORMATS else "eml")
        r = 1
        for key, label in FORMATS.items():
            ttk.Radiobutton(body, text=label, value=key, variable=self.fmt, command=self._sync).grid(
                row=r, column=1, columnspan=2, sticky="w", pady=1)
            r += 1

        ttk.Label(body, text="Save into", style="Field.TLabel").grid(row=r, column=0, sticky="w", pady=(12, 4))
        self.out = tk.StringVar(value=app.settings.get("save_dir_export") or os.path.join(default_save_dir(app.settings), "Mailex export"))
        ent = ttk.Entry(body, textvariable=self.out, width=58)
        ent.grid(row=r, column=1, sticky="ew", pady=(12, 4))
        ttk.Button(body, text="Browse…", command=self._browse).grid(row=r, column=2, sticky="w", padx=(6, 0), pady=(12, 4))
        r += 1

        st = app.settings
        self.mirror = tk.BooleanVar(value=bool(st.get("opt_mirror", True)))
        self.atts = tk.BooleanVar(value=bool(st.get("opt_atts", True)))
        self.single_pdf = tk.BooleanVar(value=bool(st.get("opt_single_pdf", False)))
        self.only_mail = tk.BooleanVar(value=bool(app.only_mail.get()))
        self.att_sub = tk.BooleanVar(value=bool(st.get("opt_att_sub", True)))
        self.vcard = tk.BooleanVar(value=bool(st.get("opt_vcard", True)))
        self.skip_inline = tk.BooleanVar(value=bool(st.get("opt_skip_inline", False)))
        self.att_ext = tk.StringVar(value=str(st.get("opt_att_ext", "")))
        self.att_kind = tk.StringVar(value=app.att_type_var.get())
        self.cb_mirror = ttk.Checkbutton(body, text="Keep the folder structure (one folder per file, then the mailbox's own folders)", variable=self.mirror)
        self.cb_mirror.grid(row=r, column=1, columnspan=2, sticky="w", pady=(8, 1)); r += 1
        self.cb_atts = ttk.Checkbutton(body, text="Include attachments (embedded in EML / MBOX; saved beside PDF, HTML and text)", variable=self.atts)
        self.cb_atts.grid(row=r, column=1, columnspan=2, sticky="w", pady=1); r += 1
        self.cb_single = ttk.Checkbutton(body, text="PDF: put every message in ONE combined PDF instead of one file each", variable=self.single_pdf)
        self.cb_single.grid(row=r, column=1, columnspan=2, sticky="w", pady=1); r += 1
        self.cb_vcard = ttk.Checkbutton(body, text="Contacts: also write a .vcf beside each contact, plus contacts.vcf and contacts.csv at the top",
                                        variable=self.vcard)
        self.cb_vcard.grid(row=r, column=1, columnspan=2, sticky="w", pady=1); r += 1
        self.cb_mail = ttk.Checkbutton(body, text="Skip calendar, contact, task and note items - e-mail only", variable=self.only_mail)
        self.cb_mail.grid(row=r, column=1, columnspan=2, sticky="w", pady=1); r += 1
        self.cb_attsub = ttk.Checkbutton(body, text="Attachments only: a folder per message (untick to pool them together)", variable=self.att_sub)
        self.cb_attsub.grid(row=r, column=1, columnspan=2, sticky="w", pady=(8, 1)); r += 1
        self.cb_inline = ttk.Checkbutton(body, text="Attachments only: skip inline pictures (signature logos, embedded images)", variable=self.skip_inline)
        self.cb_inline.grid(row=r, column=1, columnspan=2, sticky="w", pady=1); r += 1
        af = ttk.Frame(body)
        af.grid(row=r, column=1, columnspan=2, sticky="w", pady=1); r += 1
        self.lbl_kind = ttk.Label(af, text="Attachments only: keep")
        self.lbl_kind.pack(side="left")
        self.box_kind = ttk.Combobox(af, textvariable=self.att_kind, state="readonly", width=15,
                                     values=[ATT_TYPE_LABELS[k] for k in ATT_TYPES])
        self.box_kind.pack(side="left", padx=(6, 12))
        self.lbl_ext = ttk.Label(af, text="only these extensions")
        self.lbl_ext.pack(side="left")
        self.ent_ext = ttk.Entry(af, textvariable=self.att_ext, width=22)
        self.ent_ext.pack(side="left", padx=(6, 0))
        self.lbl_ext2 = ttk.Label(af, text="(e.g. pdf, docx, xlsx - blank for all)", style="Dim.TLabel")
        self.lbl_ext2.pack(side="left", padx=(6, 0))

        btns = ttk.Frame(body)
        btns.grid(row=r, column=0, columnspan=3, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Export", style="Accent.TButton", command=self._go).pack(side="right", padx=(0, 8))
        body.columnconfigure(1, weight=1)
        self._sync()
        self.bind("<Return>", lambda _e: self._go())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_width()) // 2
        y = app.root.winfo_rooty() + (app.root.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        ent.focus_set()

    def _sync(self):
        f = self.fmt.get()
        att_only = f == "attachments"
        self.cb_single.configure(state="normal" if f == "pdf" else "disabled")
        self.cb_attsub.configure(state="normal" if att_only else "disabled")
        self.cb_inline.configure(state="normal" if att_only else "disabled")
        self.box_kind.configure(state="readonly" if att_only else "disabled")
        self.ent_ext.configure(state="normal" if att_only else "disabled")
        self.cb_atts.configure(state="disabled" if att_only or f == "vcf" else "normal")
        self.cb_vcard.configure(state="disabled" if f == "vcf" else "normal")
        self.cb_mail.configure(state="disabled" if f == "vcf" else "normal")
        self.cb_mirror.configure(state="normal")

    def _browse(self):
        d = filedialog.askdirectory(title="Save the export into", initialdir=self.out.get() or default_save_dir(self.app.settings), parent=self)
        if d:
            self.out.set(d)

    def _go(self):
        out = self.out.get().strip()
        if not out:
            messagebox.showwarning(APP, "Choose a folder to save into.", parent=self)
            return
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP, f"That folder cannot be created:\n{exc}", parent=self)
            return
        kind = next((k for k, v in ATT_TYPE_LABELS.items() if v == self.att_kind.get()), "all")
        exts = parse_extensions(self.att_ext.get())
        fmt = self.fmt.get()
        opts = ExportOptions(fmt=fmt, out_dir=out, mirror_folders=bool(self.mirror.get()),
                             include_attachments=bool(self.atts.get()), pdf_single_file=bool(self.single_pdf.get()),
                             only_email=bool(self.only_mail.get()) and fmt != "vcf",
                             attachments_subfolder=bool(self.att_sub.get()),
                             contacts_vcard=bool(self.vcard.get()),
                             attachment_extensions=(exts or None) if fmt == "attachments" else None,
                             attachment_types=({kind} if kind != "all" else None) if fmt == "attachments" else None,
                             skip_inline_images=bool(self.skip_inline.get()) and fmt == "attachments")
        self.app.settings.update(opt_mirror=opts.mirror_folders, opt_atts=opts.include_attachments,
                                 opt_single_pdf=opts.pdf_single_file, opt_att_sub=opts.attachments_subfolder,
                                 opt_vcard=bool(self.vcard.get()), opt_skip_inline=bool(self.skip_inline.get()),
                                 opt_att_ext=self.att_ext.get().strip())
        self.destroy()
        self.app.run_export(self.rows, opts)


class StatsWindow(tk.Toplevel):
    """View > Statistics: tables over the rows already listed (or everything
    open), each on its own tab, with CSV and HTML export."""

    def __init__(self, app: App):
        super().__init__(app.root)
        self.app = app
        self.title("Mailbox statistics")
        self.geometry("980x640")
        self.configure(bg=app.c["bg"])
        self.transient(app.root)
        self.stats = None
        self.rows: List[MessageRow] = []
        top = ttk.Frame(self, padding=(14, 12, 14, 6))
        top.pack(side="top", fill="x")
        ttk.Label(top, text="Statistics for", style="Field.TLabel").pack(side="left")
        n_listed = len(app.current_rows)
        self.scope = tk.StringVar(value=f"the listed folder ({n_listed:,} items)" if n_listed else "everything open")
        self.scope_box = ttk.Combobox(top, textvariable=self.scope, state="readonly", width=34,
                                      values=[f"the listed folder ({n_listed:,} items)", "everything open"])
        self.scope_box.pack(side="left", padx=(8, 14))
        self.scope_box.bind("<<ComboboxSelected>>", lambda _e: self._compute())
        self.att_btn = ttk.Button(top, text="Read attachment details…", command=self._read_attachments)
        self.att_btn.pack(side="left")
        ttk.Button(top, text="Save as HTML…", command=self._save_html).pack(side="right")
        ttk.Button(top, text="Save as CSV…", command=self._save_csv).pack(side="right", padx=(0, 8))
        self.nb = ttk.Notebook(self)
        self.nb.pack(side="top", fill="both", expand=True, padx=14, pady=(4, 6))
        self.status = ttk.Label(self, text="", style="Dim.TLabel", padding=(16, 2, 16, 8))
        self.status.pack(side="bottom", fill="x")
        self.bind("<Escape>", lambda _e: self.destroy())
        self._compute()

    def _scope_rows(self):
        if self.scope.get().startswith("the listed"):
            return list(self.app.current_rows), (self.app.current_folder.path_str if self.app.current_folder else "listed folder")
        return None, "everything open"

    def _att_info(self, row):
        d = self.app._details.get(id(row))
        if d and d[2] is not None:
            return [(name, size) for name, inline, size in d[2] if not inline]
        return None

    def _compute(self):
        from . import stats as _stats
        rows, label = self._scope_rows()
        self.status.configure(text="Working…")

        def work():
            r = rows if rows is not None else [x for pst in self.app.files for x in pst.all_message_rows()]
            files = ", ".join(f.name for f in self.app.files)
            return r, _stats.compute(r, f"{files} - {label}", att_info=self._att_info)

        def done(res):
            try:
                if isinstance(res, BaseException):
                    self.status.configure(text=f"Could not compute: {res}")
                    return
                self.rows, self.stats = res
                self._render()
            except tk.TclError:
                pass            # the window was closed while the numbers were being added up

        self.app._run_bg(work, done)

    def _render(self):
        for tab in self.nb.tabs():
            old = self.nametowidget(tab)
            self.nb.forget(tab)
            old.destroy()
        for t in self.stats.tables:
            frame = ttk.Frame(self.nb, padding=(6, 6))
            self.nb.add(frame, text=t.title)
            if t.note:
                ttk.Label(frame, text=t.note, style="Dim.TLabel", wraplength=900).pack(side="top", anchor="w", pady=(0, 4))
            cols = list(range(len(t.columns))) + ([len(t.columns)] if t.bar_column is not None else [])
            tv = ttk.Treeview(frame, columns=[f"c{i}" for i in cols], show="headings")
            biggest = 0.0
            if t.bar_column is not None:
                biggest = max((float(r[t.bar_column]) for r in t.rows if isinstance(r[t.bar_column], (int, float))), default=0.0)
            for i in cols:
                if i < len(t.columns):
                    numeric = all(isinstance(r[i], (int, float)) for r in t.rows) if t.rows else False
                    width = 110 if numeric else (360 if i == 0 or t.columns[i] in ("Subject", "Folder") else 200)
                    tv.heading(f"c{i}", text=t.columns[i])
                    tv.column(f"c{i}", width=width, anchor="e" if numeric else "w", stretch=not numeric)
                else:
                    tv.heading(f"c{i}", text="")
                    tv.column(f"c{i}", width=220, anchor="w", stretch=False)
            for r in t.rows:
                vals = [f"{v:,}" if isinstance(v, int) else str(v) for v in r]
                if t.bar_column is not None:
                    v = r[t.bar_column]
                    k = int(round(30 * float(v) / biggest)) if (biggest and isinstance(v, (int, float))) else 0
                    vals.append("\u2588" * k)
                tv.insert("", "end", values=vals)
            sb = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
            tv.configure(yscrollcommand=sb.set)
            tv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
        self.status.configure(text=f"{self.stats.total:,} items - {self.stats.label}")

    def _read_attachments(self):
        """Open the messages that have attachments so the counts and sizes are real."""
        todo = [r for r in self.rows if r.has_attachments and self._att_info(r) is None]
        if not todo:
            self.status.configure(text="Attachment details are already complete for this scope.")
            return
        self.att_btn.configure(state="disabled")
        self.status.configure(text=f"Reading attachment lists of {len(todo):,} messages…")

        def work():
            for r in todo:
                entry = self.app._details.setdefault(id(r), [r, None, None])
                if entry[2] is not None:
                    continue
                try:
                    entry[2] = [(a.filename, bool(a.hidden or a.is_inline), a.size or len(a.data or b""))
                                for a in r.open().attachments()]
                except Exception:  # noqa: BLE001
                    entry[2] = []
            return True

        def done(_res):
            try:
                self.att_btn.configure(state="normal")
                self._compute()
            except tk.TclError:
                pass

        self.app._run_bg(work, done)

    def _save_csv(self):
        from . import stats as _stats
        if self.stats is None:
            return
        d = filedialog.askdirectory(title="Save one CSV per table into", initialdir=default_save_dir(self.app.settings), parent=self)
        if not d:
            return
        paths = _stats.write_csv_dir(self.stats, d)
        self.status.configure(text=f"{len(paths)} CSV files written to {d}")

    def _save_html(self):
        from . import stats as _stats
        if self.stats is None:
            return
        path = filedialog.asksaveasfilename(title="Save statistics as HTML", defaultextension=".html",
                                            initialfile="mailbox-statistics.html", filetypes=[("Web page", "*.html")],
                                            initialdir=default_save_dir(self.app.settings), parent=self)
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_stats.render_html(self.stats))
        self.status.configure(text=f"Saved {os.path.basename(path)}")


class ProgressWindow(tk.Toplevel):
    def __init__(self, app: App, total: int, cancel: threading.Event, verb: str = "Exporting"):
        super().__init__(app.root)
        self.cancel = cancel
        self.verb = verb
        self.title(f"{verb}…")
        self.configure(bg=app.c["bg"])
        self.resizable(False, False)
        self.transient(app.root)
        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        self.label = ttk.Label(body, text=f"{verb} 0 of {total}…", width=70)
        self.label.pack(anchor="w")
        self.bar = ttk.Progressbar(body, length=520, mode="determinate", maximum=max(1, total))
        self.bar.pack(fill="x", pady=(8, 8))
        self.detail = ttk.Label(body, text="", style="Dim.TLabel", width=70)
        self.detail.pack(anchor="w")
        ttk.Button(body, text="Cancel", command=self._cancel).pack(anchor="e", pady=(10, 0))
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        # modal: closing files or quitting under a running export would corrupt it
        self.after(100, self._grab)
        self.update_idletasks()
        x = app.root.winfo_rootx() + (app.root.winfo_width() - self.winfo_width()) // 2
        y = app.root.winfo_rooty() + (app.root.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.total = total

    def _grab(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def update_progress(self, i: int, n: int, label: str):
        try:
            self.bar.configure(value=i, maximum=max(1, n))
            self.label.configure(text=f"{self.verb} {i} of {n}…" if n else f"{self.verb}…")
            self.detail.configure(text=(label or "")[:90])
        except tk.TclError:
            pass

    def _cancel(self):
        self.cancel.set()
        self.label.configure(text="Cancelling after the current message…")

    def close(self):
        try:
            self.destroy()
        except tk.TclError:
            pass


def launch(paths: Optional[List[str]] = None):
    root = dnd.make_root()          # a TkinterDnD root when the package is there, plain Tk otherwise
    App(root, paths)
    root.mainloop()
