# -*- coding: utf-8 -*-
"""
Desktop GUI for Crypto Startup Radar.

The interface starts crawler.py or worker.py as a separate process, streams logs,
shows SQLite results, and opens the generated report.
"""

from __future__ import annotations

import os
import queue
import json
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox
import tkinter as tk
from tkinter import ttk


ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_FILE = "logs/crawler_gui.log"

BG = "#0b1020"
PANEL = "#111827"
PANEL_2 = "#172033"
FIELD = "#0f172a"
TEXT = "#e5eefb"
MUTED = "#94a3b8"
ACCENT = "#38bdf8"
ACCENT_2 = "#22c55e"
DANGER = "#ef4444"
WARN = "#f59e0b"
BORDER = "#243247"

CATEGORIES = [
    "Все",
    "DeFi",
    "L1/L2",
    "Infrastructure",
    "Dev Tooling",
    "AI+Crypto",
    "GameFi",
    "RWA",
    "ZK",
    "DA Layer",
    "Stablecoin",
    "Other",
    "Filtered",
]

PROFILE_SCOPES = [
    "Все профили",
    "Стартапы",
    "AI pending",
    "Отфильтрованные",
    "Не стартапы",
    "Без AI-вердикта",
]


def resolve_path(value: str, default: str) -> Path:
    raw = (value or default).strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    return path


class RadarGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Crypto Startup Radar")
        self.geometry("1280x820")
        self.minsize(1080, 720)
        self.configure(bg=BG)

        self.process: subprocess.Popen[str] | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.details_by_iid: dict[str, dict] = {}
        self.last_raw_rows: list[dict] = []
        self.log_warning_count = 0
        self.log_error_count = 0
        self._log_file_error_shown = False

        self._create_vars()
        self._configure_style()
        self._build_layout()
        self.refresh_all()
        self.after(120, self._drain_log_queue)
        self.after(10000, self._periodic_refresh)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _create_vars(self) -> None:
        self.mode_var = tk.StringVar(value="Standalone crawler")
        self.status_var = tk.StringVar(value="Готов к запуску")
        self.search_var = tk.StringVar()
        self.category_var = tk.StringVar(value="Все")
        self.scope_var = tk.StringVar(value="Все профили")

        self.depth1_var = tk.StringVar(value="50")
        self.depth2_var = tk.StringVar(value="60")
        self.concurrent_var = tk.StringVar(value="3")
        self.scroll_var = tk.StringVar(value="5")
        self.scroll_min_var = tk.StringVar(value="2")
        self.cache_ttl_var = tk.StringVar(value="72")
        self.ai_ttl_var = tk.StringVar(value="168")
        self.rps_var = tk.StringVar(value="1.5")
        self.model_var = tk.StringVar(value="llama3.1:8b")
        self.db_var = tk.StringVar(value=os.environ.get("STARTUPS_DB", "startups.db"))
        self.report_var = tk.StringVar(value=os.environ.get("CSR_REPORT_FILE", "report.md"))
        self.log_file_var = tk.StringVar(value=os.environ.get("CSR_GUI_LOG_FILE", DEFAULT_LOG_FILE))
        self.redis_var = tk.StringVar(value=os.environ.get("REDIS_URL", ""))
        self.headless_var = tk.BooleanVar(value=True)
        self.report_raw_var = tk.BooleanVar(value=False)
        self.view_report_var = tk.StringVar(value="Текущий")
        self._last_report_content = ""
        self._last_report_raw = False

        self.total_profiles_var = tk.StringVar(value="0")
        self.startups_var = tk.StringVar(value="0")
        self.ai_due_var = tk.StringVar(value="0")
        self.filtered_var = tk.StringVar(value="0")
        self.tweets_var = tk.StringVar(value="0")
        self.mentions_var = tk.StringVar(value="0")
        self.last_update_var = tk.StringVar(value="нет данных")
        self.db_state_var = tk.StringVar(value="База не найдена")
        self.log_summary_var = tk.StringVar(value="Warnings: 0  Errors: 0")

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, relief="flat")
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Card.TLabel", background=PANEL, foreground=TEXT)
        style.configure("MutedCard.TLabel", background=PANEL, foreground=MUTED)
        style.configure(
            "TEntry",
            fieldbackground=FIELD,
            foreground=TEXT,
            insertcolor=TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
        )
        style.configure(
            "TCombobox",
            fieldbackground=FIELD,
            background=FIELD,
            foreground=TEXT,
            arrowcolor=TEXT,
            bordercolor=BORDER,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", FIELD)],
            foreground=[("readonly", TEXT)],
        )
        style.configure(
            "Treeview",
            background=FIELD,
            fieldbackground=FIELD,
            foreground=TEXT,
            bordercolor=BORDER,
            rowheight=30,
        )
        style.configure(
            "Treeview.Heading",
            background=PANEL_2,
            foreground=TEXT,
            relief="flat",
            font=("Segoe UI Semibold", 10),
        )
        style.map("Treeview", background=[("selected", "#0ea5e9")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=PANEL,
            foreground=MUTED,
            padding=(18, 8),
            borderwidth=0,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", PANEL_2)],
            foreground=[("selected", TEXT)],
        )
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", PANEL)], foreground=[("active", TEXT)])

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = tk.Frame(self, bg=BG, padx=24, pady=18)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(0, weight=1)

        tk.Label(
            header,
            text="Crypto Startup Radar",
            bg=BG,
            fg=TEXT,
            font=("Segoe UI Semibold", 22),
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            header,
            text="Панель запуска, мониторинга и разбора найденных web3-стартапов",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        self.status_pill = tk.Label(
            header,
            textvariable=self.status_var,
            bg="#132235",
            fg=ACCENT,
            padx=16,
            pady=8,
            font=("Segoe UI Semibold", 10),
        )
        self.status_pill.grid(row=0, column=1, rowspan=2, sticky="e")

        sidebar = tk.Frame(self, bg=BG, padx=20, pady=0)
        sidebar.grid(row=1, column=0, sticky="ns")
        sidebar.grid_rowconfigure(2, weight=1)

        self._build_controls(sidebar)
        self._build_settings(sidebar)
        self._build_quick_stats(sidebar)

        main = tk.Frame(self, bg=BG)
        main.grid(row=1, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        self._build_metric_cards(main)
        self._build_notebook(main)

    def _build_controls(self, parent: tk.Frame) -> None:
        card = self._card(parent)
        card.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        card.grid_columnconfigure(0, weight=1)

        self._card_title(card, "Запуск").grid(row=0, column=0, sticky="w", padx=16, pady=(14, 8))
        ttk.Combobox(
            card,
            textvariable=self.mode_var,
            values=["Standalone crawler", "Redis worker"],
            state="readonly",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

        btn_row = tk.Frame(card, bg=PANEL)
        btn_row.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 12))
        btn_row.grid_columnconfigure((0, 1), weight=1)
        self.start_btn = self._button(btn_row, "Запустить", self.start_crawler, ACCENT_2)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.stop_btn = self._button(btn_row, "Остановить", self.stop_crawler, DANGER)
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

        util_row = tk.Frame(card, bg=PANEL)
        util_row.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        util_row.grid_columnconfigure((0, 1), weight=1)
        self._button(util_row, "Авторизация X", self.open_auth_setup, ACCENT).grid(
            row=0, column=0, sticky="ew", padx=(0, 6)
        )
        self._button(util_row, "Обновить", self.refresh_all, "#64748b").grid(
            row=0, column=1, sticky="ew", padx=(6, 0)
        )

    def _build_settings(self, parent: tk.Frame) -> None:
        card = self._card(parent)
        card.grid(row=1, column=0, sticky="ew", pady=(0, 14))
        card.grid_columnconfigure(1, weight=1)

        self._card_title(card, "Настройки").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(14, 8)
        )

        fields = [
            ("Depth 1", self.depth1_var),
            ("Depth 2", self.depth2_var),
            ("Параллельно", self.concurrent_var),
            ("Scroll", self.scroll_var),
            ("Min scroll", self.scroll_min_var),
            ("Cache TTL, ч", self.cache_ttl_var),
            ("AI TTL, ч", self.ai_ttl_var),
            ("RPS", self.rps_var),
            ("Ollama model", self.model_var),
            ("SQLite DB", self.db_var),
            ("Report", self.report_var),
            ("Redis URL", self.redis_var),
        ]
        for idx, (label, var) in enumerate(fields, start=1):
            tk.Label(card, text=label, bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).grid(
                row=idx, column=0, sticky="w", padx=(16, 10), pady=4
            )
            entry = ttk.Entry(card, textvariable=var)
            entry.grid(row=idx, column=1, sticky="ew", padx=(0, 8), pady=4)
            if label == "SQLite DB":
                self._small_button(card, "...", self.browse_db).grid(
                    row=idx, column=2, sticky="ew", padx=(0, 16), pady=4
                )
            elif label == "Report":
                self._small_button(card, "...", self.browse_report).grid(
                    row=idx, column=2, sticky="ew", padx=(0, 16), pady=4
                )
            else:
                tk.Frame(card, width=26, bg=PANEL).grid(row=idx, column=2, padx=(0, 16))

        ttk.Checkbutton(
            card,
            text="Headless browser",
            variable=self.headless_var,
        ).grid(row=len(fields) + 1, column=0, columnspan=3, sticky="w", padx=16, pady=(8, 14))

    def _build_quick_stats(self, parent: tk.Frame) -> None:
        card = self._card(parent)
        card.grid(row=2, column=0, sticky="new")
        card.grid_columnconfigure(0, weight=1)

        self._card_title(card, "Состояние").grid(row=0, column=0, sticky="w", padx=16, pady=(14, 8))
        tk.Label(card, textvariable=self.db_state_var, bg=PANEL, fg=MUTED, wraplength=270, justify="left").grid(
            row=1, column=0, sticky="w", padx=16
        )
        tk.Label(card, text="Последняя запись", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).grid(
            row=2, column=0, sticky="w", padx=16, pady=(14, 0)
        )
        tk.Label(card, textvariable=self.last_update_var, bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 10)).grid(
            row=3, column=0, sticky="w", padx=16, pady=(2, 16)
        )

        actions = tk.Frame(card, bg=PANEL)
        actions.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 16))
        actions.grid_columnconfigure((0, 1), weight=1)
        self._button(actions, "Открыть отчет", self.open_report_file, ACCENT).grid(
            row=0, column=0, sticky="ew", padx=(0, 6)
        )
        self._button(actions, "Открыть БД", self.open_db_file, "#64748b").grid(
            row=0, column=1, sticky="ew", padx=(6, 0)
        )

    def _build_metric_cards(self, parent: tk.Frame) -> None:
        row = tk.Frame(parent, bg=BG)
        row.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        row.grid_columnconfigure((0, 1, 2, 3, 4, 5), weight=1, uniform="metrics")

        self._metric_card(row, "Профили", self.total_profiles_var, ACCENT).grid(
            row=0, column=0, sticky="ew", padx=(0, 10)
        )
        self._metric_card(row, "Стартапы", self.startups_var, ACCENT_2).grid(
            row=0, column=1, sticky="ew", padx=(0, 10)
        )
        self._metric_card(row, "AI pending", self.ai_due_var, WARN).grid(
            row=0, column=2, sticky="ew", padx=(0, 10)
        )
        self._metric_card(row, "Filtered", self.filtered_var, "#f97316").grid(
            row=0, column=3, sticky="ew", padx=(0, 10)
        )
        self._metric_card(row, "Твиты", self.tweets_var, "#a78bfa").grid(
            row=0, column=4, sticky="ew", padx=(0, 10)
        )
        self._metric_card(row, "Упоминания", self.mentions_var, "#f472b6").grid(
            row=0, column=5, sticky="ew"
        )

    def _build_notebook(self, parent: tk.Frame) -> None:
        self.notebook = ttk.Notebook(parent)
        self.notebook.grid(row=1, column=0, sticky="nsew")

        overview_tab = tk.Frame(self.notebook, bg=BG)
        results_tab = tk.Frame(self.notebook, bg=BG)
        logs_tab = tk.Frame(self.notebook, bg=BG)
        report_tab = tk.Frame(self.notebook, bg=BG)
        data_tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(overview_tab, text="Обзор")
        self.notebook.add(results_tab, text="Профили")
        self.notebook.add(logs_tab, text="Логи")
        self.notebook.add(report_tab, text="Отчет")
        self.notebook.add(data_tab, text="Данные")

        self._build_overview_tab(overview_tab)
        self._build_results_tab(results_tab)
        self._build_logs_tab(logs_tab)
        self._build_report_tab(report_tab)
        self._build_data_tab(data_tab)

    def _build_overview_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        toolbar = tk.Frame(parent, bg=BG, pady=10)
        toolbar.grid(row=0, column=0, sticky="ew")
        self._button(toolbar, "Обновить обзор", self.refresh_overview, ACCENT).grid(row=0, column=0, sticky="w")

        wrap = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        self.overview_text = tk.Text(
            wrap,
            bg=FIELD,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            wrap="word",
            padx=16,
            pady=16,
            font=("Segoe UI", 10),
        )
        self.overview_text.grid(row=0, column=0, sticky="nsew")
        self.overview_text.tag_configure("section", foreground=ACCENT, font=("Segoe UI Semibold", 13))
        self.overview_text.tag_configure("muted", foreground=MUTED)
        self.overview_text.configure(state="disabled")

        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.overview_text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.overview_text.configure(yscrollcommand=yscroll.set)

    def _build_results_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=3)
        parent.grid_columnconfigure(1, weight=2)
        parent.grid_rowconfigure(1, weight=1)

        filters = tk.Frame(parent, bg=BG, pady=10)
        filters.grid(row=0, column=0, columnspan=2, sticky="ew")
        filters.grid_columnconfigure(5, weight=1)

        tk.Label(filters, text="Показать", bg=BG, fg=MUTED).grid(row=0, column=0, sticky="w", padx=(0, 8))
        scope = ttk.Combobox(filters, textvariable=self.scope_var, values=PROFILE_SCOPES, state="readonly", width=18)
        scope.grid(row=0, column=1, sticky="w")
        scope.bind("<<ComboboxSelected>>", lambda _event: self.refresh_results())

        tk.Label(filters, text="Категория", bg=BG, fg=MUTED).grid(row=0, column=2, sticky="w", padx=(18, 8))
        category = ttk.Combobox(filters, textvariable=self.category_var, values=CATEGORIES, state="readonly", width=18)
        category.grid(row=0, column=3, sticky="w")
        category.bind("<<ComboboxSelected>>", lambda _event: self.refresh_results())

        tk.Label(filters, text="Поиск", bg=BG, fg=MUTED).grid(row=0, column=4, sticky="e", padx=(18, 8))
        search = ttk.Entry(filters, textvariable=self.search_var, width=34)
        search.grid(row=0, column=5, sticky="ew")
        search.bind("<Return>", lambda _event: self.refresh_results())
        self._button(filters, "Найти", self.refresh_results, ACCENT).grid(row=0, column=6, padx=(10, 0))

        table_wrap = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        table_wrap.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        table_wrap.grid_rowconfigure(0, weight=1)
        table_wrap.grid_columnconfigure(0, weight=1)

        columns = ("status", "username", "category", "stage", "depth", "tweets", "mentions", "parsed_at")
        self.results_tree = ttk.Treeview(table_wrap, columns=columns, show="headings", selectmode="browse")
        headings = {
            "status": "Статус",
            "username": "@account",
            "category": "Категория",
            "stage": "Стадия",
            "depth": "Depth",
            "tweets": "Твиты",
            "mentions": "Mentions",
            "parsed_at": "Дата",
        }
        widths = {
            "status": 110,
            "username": 150,
            "category": 125,
            "stage": 110,
            "depth": 60,
            "tweets": 65,
            "mentions": 80,
            "parsed_at": 155,
        }
        for col in columns:
            self.results_tree.heading(col, text=headings[col])
            self.results_tree.column(col, width=widths[col], anchor="w")
        self.results_tree.grid(row=0, column=0, sticky="nsew")
        self.results_tree.bind("<<TreeviewSelect>>", self._on_result_selected)

        yscroll = ttk.Scrollbar(table_wrap, orient="vertical", command=self.results_tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.results_tree.configure(yscrollcommand=yscroll.set)

        detail_wrap = tk.Frame(parent, bg=PANEL, padx=14, pady=14)
        detail_wrap.grid(row=1, column=1, sticky="nsew")
        detail_wrap.grid_rowconfigure(1, weight=1)
        detail_wrap.grid_columnconfigure(0, weight=1)

        detail_header = tk.Frame(detail_wrap, bg=PANEL)
        detail_header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        detail_header.grid_columnconfigure(0, weight=1)
        tk.Label(
            detail_header,
            text="Карточка профиля",
            bg=PANEL,
            fg=TEXT,
            font=("Segoe UI Semibold", 13),
        ).grid(row=0, column=0, sticky="w")
        self._small_button(detail_header, "X.com", self.open_selected_url).grid(row=0, column=1, sticky="e")

        self.detail_text = tk.Text(
            detail_wrap,
            bg=FIELD,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            wrap="word",
            padx=12,
            pady=12,
            font=("Segoe UI", 10),
            height=12,
        )
        self.detail_text.grid(row=1, column=0, sticky="nsew")
        self.detail_text.configure(state="disabled")

    def _build_logs_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        toolbar = tk.Frame(parent, bg=BG, pady=10)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_columnconfigure(4, weight=1)
        self._button(toolbar, "Очистить экран", self.clear_logs, "#64748b").grid(row=0, column=0, sticky="w")
        self._button(toolbar, "Перечитать файл", self.reload_log_file, ACCENT).grid(row=0, column=1, sticky="w", padx=(10, 0))
        self._button(toolbar, "Открыть файл", self.open_log_file, "#64748b").grid(row=0, column=2, sticky="w", padx=(10, 0))
        tk.Label(toolbar, textvariable=self.log_summary_var, bg=BG, fg=MUTED).grid(row=0, column=4, sticky="e")
        tk.Label(
            toolbar,
            textvariable=self.log_file_var,
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 8),
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        wrap = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            wrap,
            bg="#050816",
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            wrap="word",
            padx=12,
            pady=12,
            font=("Consolas", 10),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.tag_configure("error", foreground="#fca5a5")
        self.log_text.tag_configure("warn", foreground="#fcd34d")
        self.log_text.tag_configure("ok", foreground="#86efac")
        self.log_text.tag_configure("phase", foreground="#7dd3fc")
        self.log_text.tag_configure("muted", foreground=MUTED)
        self.log_text.configure(state="disabled")

        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.log_text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=yscroll.set)

    def _build_report_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        toolbar = tk.Frame(parent, bg=BG, pady=10)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.grid_columnconfigure(4, weight=1)
        self._button(toolbar, "Перечитать отчет", self.refresh_report, ACCENT).grid(row=0, column=0, padx=(0, 10))
        self._button(toolbar, "Открыть файл", self.open_report_file, "#64748b").grid(row=0, column=1)
        
        self.report_combo = ttk.Combobox(toolbar, textvariable=self.view_report_var, state="readonly", width=35)
        self.report_combo.grid(row=0, column=2, sticky="w", padx=(10, 0))
        self.report_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_report())

        tk.Checkbutton(
            toolbar,
            text="Показать Markdown",
            variable=self.report_raw_var,
            command=self.refresh_report,
            bg=BG,
            fg=MUTED,
            activebackground=BG,
            activeforeground=TEXT,
            selectcolor=FIELD,
            relief="flat",
            font=("Segoe UI", 9),
        ).grid(row=0, column=3, sticky="w", padx=(14, 0))

        wrap = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        self.report_text = tk.Text(
            wrap,
            bg=FIELD,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            wrap="word",
            padx=14,
            pady=14,
            font=("Segoe UI", 10),
        )
        self.report_text.grid(row=0, column=0, sticky="nsew")
        self.report_text.tag_configure("h1", foreground=TEXT, font=("Segoe UI Semibold", 20), spacing1=4, spacing3=12)
        self.report_text.tag_configure("h2", foreground=ACCENT, font=("Segoe UI Semibold", 15), spacing1=12, spacing3=8)
        self.report_text.tag_configure("h3", foreground=ACCENT_2, font=("Segoe UI Semibold", 12), spacing1=8, spacing3=4)
        self.report_text.tag_configure("quote", foreground="#c4b5fd", lmargin1=18, lmargin2=18, spacing1=2, spacing3=4)
        self.report_text.tag_configure("meta", foreground=MUTED)
        self.report_text.tag_configure("separator", foreground=BORDER, spacing1=8, spacing3=8)
        self.report_text.configure(state="disabled")

        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.report_text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        self.report_text.configure(yscrollcommand=yscroll.set)

    def _build_data_tab(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        toolbar = tk.Frame(parent, bg=BG, pady=10)
        toolbar.grid(row=0, column=0, sticky="ew")
        self._button(toolbar, "Обновить данные", self.refresh_data, ACCENT).grid(row=0, column=0, padx=(0, 10))
        self._button(toolbar, "Экспорт JSON", self.export_data_json, "#64748b").grid(row=0, column=1)

        wrap = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        self.data_text = tk.Text(
            wrap,
            bg="#050816",
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            wrap="none",
            padx=12,
            pady=12,
            font=("Consolas", 10),
        )
        self.data_text.grid(row=0, column=0, sticky="nsew")
        self.data_text.configure(state="disabled")

        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.data_text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(wrap, orient="horizontal", command=self.data_text.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.data_text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

    def _card(self, parent: tk.Frame) -> tk.Frame:
        frame = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        frame.configure(width=320)
        return frame

    def _card_title(self, parent: tk.Frame, text: str) -> tk.Label:
        return tk.Label(parent, text=text, bg=PANEL, fg=TEXT, font=("Segoe UI Semibold", 13))

    def _metric_card(self, parent: tk.Frame, label: str, value_var: tk.StringVar, color: str) -> tk.Frame:
        frame = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, padx=16, pady=14)
        frame.grid_columnconfigure(0, weight=1)
        tk.Label(frame, text=label, bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w")
        tk.Label(frame, textvariable=value_var, bg=PANEL, fg=color, font=("Segoe UI Semibold", 24)).grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        return frame

    def _button(self, parent: tk.Misc, text: str, command, color: str) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=color,
            fg="#06101f" if color in {ACCENT, ACCENT_2, WARN} else TEXT,
            activebackground=color,
            activeforeground="#06101f" if color in {ACCENT, ACCENT_2, WARN} else TEXT,
            relief="flat",
            padx=12,
            pady=8,
            cursor="hand2",
            font=("Segoe UI Semibold", 9),
        )

    def _small_button(self, parent: tk.Misc, text: str, command) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=PANEL_2,
            fg=TEXT,
            activebackground="#22304a",
            activeforeground=TEXT,
            relief="flat",
            padx=8,
            pady=4,
            cursor="hand2",
            font=("Segoe UI Semibold", 9),
        )

    def browse_db(self) -> None:
        selected = filedialog.asksaveasfilename(
            initialdir=ROOT,
            initialfile=self.db_var.get() or "startups.db",
            filetypes=[("SQLite DB", "*.db *.sqlite *.sqlite3"), ("All files", "*.*")],
        )
        if selected:
            self.db_var.set(os.path.relpath(selected, ROOT) if Path(selected).is_relative_to(ROOT) else selected)

    def browse_report(self) -> None:
        selected = filedialog.asksaveasfilename(
            initialdir=ROOT,
            initialfile=self.report_var.get() or "report.md",
            filetypes=[("Markdown", "*.md"), ("All files", "*.*")],
        )
        if selected:
            self.report_var.set(os.path.relpath(selected, ROOT) if Path(selected).is_relative_to(ROOT) else selected)

    def start_crawler(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showinfo("Уже запущено", "Краулер уже работает.")
            return

        if self.mode_var.get() == "Redis worker" and not self.redis_var.get().strip():
            messagebox.showerror(
                "Redis URL required",
                "Set Redis URL, for example: redis://127.0.0.1:6379/0",
            )
            return

        try:
            env = self._build_process_env()
        except ValueError as exc:
            messagebox.showerror("Некорректные настройки", str(exc))
            return

        script = "worker.py" if self.mode_var.get() == "Redis worker" else "crawler.py"
        command = [sys.executable, "-u", str(ROOT / script)]

        self.append_log(f"\n--- Запуск: {' '.join(command)} ---\n", tag="phase")
        self.status_var.set("Запущено")
        self.status_pill.configure(bg="#10251c", fg=ACCENT_2)

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as exc:
            self.status_var.set("Ошибка запуска")
            self.status_pill.configure(bg="#2a1216", fg="#fca5a5")
            messagebox.showerror("Не удалось запустить", str(exc))
            return

        self.process = process
        threading.Thread(target=self._read_process_output, args=(process,), daemon=True).start()
        self.after(500, lambda proc=process: self._poll_process(proc))

    def stop_crawler(self) -> None:
        if not self.process or self.process.poll() is not None:
            self.status_var.set("Не запущено")
            return
        self.append_log("\n--- Остановка процесса ---\n", tag="warn")
        self.status_var.set("Остановка")
        self.status_pill.configure(bg="#2b2111", fg=WARN)
        process = self.process
        process.terminate()
        self.after(5000, lambda proc=process: self._kill_if_alive(proc))

    def open_auth_setup(self) -> None:
        script = ROOT / "auth_setup.py"
        if not script.exists():
            messagebox.showerror("Файл не найден", str(script))
            return
        try:
            subprocess.Popen([sys.executable, str(script)], cwd=ROOT)
            self.append_log("Открыт браузер для авторизации X. Закройте окно браузера после входа.\n", tag="phase")
        except OSError as exc:
            messagebox.showerror("Не удалось открыть авторизацию", str(exc))

    def _build_process_env(self) -> dict[str, str]:
        depth1 = self._require_int(self.depth1_var, "Depth 1", minimum=0)
        depth2 = self._require_int(self.depth2_var, "Depth 2", minimum=0)
        concurrent = self._require_int(self.concurrent_var, "Параллельно", minimum=1)
        scroll = self._require_int(self.scroll_var, "Scroll", minimum=1)
        scroll_min = self._require_int(self.scroll_min_var, "Min scroll", minimum=1)
        cache_ttl = self._require_int(self.cache_ttl_var, "Cache TTL", minimum=0)
        ai_ttl = self._require_int(self.ai_ttl_var, "AI TTL", minimum=0)
        rps = self._require_float(self.rps_var, "RPS", minimum=0.1)

        if scroll_min > scroll:
            raise ValueError("Min scroll не может быть больше Scroll.")
        if cache_ttl > 0 and ai_ttl > 0 and ai_ttl <= cache_ttl:
            raise ValueError("AI TTL должен быть больше Cache TTL.")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["CSR_MAX_DEPTH1"] = str(depth1)
        env["CSR_MAX_DEPTH2"] = str(depth2)
        env["CSR_MAX_CONCURRENT"] = str(concurrent)
        env["CSR_SCROLL_ROUNDS"] = str(scroll)
        env["CSR_SCROLL_ROUNDS_MIN"] = str(scroll_min)
        env["CSR_CACHE_TTL_HOURS"] = str(cache_ttl)
        env["CSR_AI_TTL_HOURS"] = str(ai_ttl)
        env["CSR_RATE_LIMIT_RPS"] = str(rps)
        env["CSR_HEADLESS"] = "1" if self.headless_var.get() else "0"
        env["CSR_OLLAMA_MODEL"] = self.model_var.get().strip() or "gemma2:27b"
        env["STARTUPS_DB"] = str(resolve_path(self.db_var.get(), "startups.db"))
        env["CSR_REPORT_FILE"] = str(resolve_path(self.report_var.get(), "report.md"))

        redis_url = self.redis_var.get().strip()
        if redis_url:
            env["REDIS_URL"] = redis_url
        else:
            env.pop("REDIS_URL", None)
        return env

    def _require_int(self, var: tk.StringVar, label: str, minimum: int) -> int:
        try:
            value = int(var.get().strip())
        except ValueError as exc:
            raise ValueError(f"{label}: нужно целое число.") from exc
        if value < minimum:
            raise ValueError(f"{label}: минимальное значение {minimum}.")
        return value

    def _require_float(self, var: tk.StringVar, label: str, minimum: float) -> float:
        try:
            value = float(var.get().strip().replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{label}: нужно число.") from exc
        if value < minimum:
            raise ValueError(f"{label}: минимальное значение {minimum}.")
        return value

    def _read_process_output(self, process: subprocess.Popen[str]) -> None:
        if not process.stdout:
            return
        for line in process.stdout:
            self.log_queue.put(line)

    def _drain_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            tag = self._tag_for_log_line(line)
            self.append_log(line, tag=tag)
        self.after(120, self._drain_log_queue)

    def _tag_for_log_line(self, line: str) -> str | None:
        upper = line.upper()
        if "ERROR" in upper or "CRITICAL" in upper or "TRACEBACK" in upper:
            return "error"
        if "WARNING" in upper or " 429" in upper or " 403" in upper:
            return "warn"
        if "STARTUP" in upper or "УСПЕШНО" in upper:
            return "ok"
        if "[1/4]" in line or "[2/4]" in line or "[3/4]" in line or "[4/4]" in line:
            return "phase"
        return None

    def append_log(self, text: str, tag: str | None = None) -> None:
        self._insert_log_text(text, tag)
        self._write_log_file(text)
        self._record_log_line(text, tag)

    def _insert_log_text(self, text: str, tag: str | None = None) -> None:
        self.log_text.configure(state="normal")
        if tag:
            self.log_text.insert("end", text, tag)
        else:
            self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _record_log_line(self, line: str, tag: str | None) -> None:
        if tag == "warn":
            self.log_warning_count += 1
        elif tag == "error":
            self.log_error_count += 1
        self.log_summary_var.set(f"Warnings: {self.log_warning_count}  Errors: {self.log_error_count}")

    def _write_log_file(self, text: str) -> None:
        try:
            path = resolve_path(self.log_file_var.get(), DEFAULT_LOG_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as file:
                file.write(text)
        except OSError as exc:
            if not self._log_file_error_shown:
                self._log_file_error_shown = True
                self._insert_log_text(f"\nНе удалось записать лог-файл: {exc}\n", "warn")

    def clear_logs(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.log_warning_count = 0
        self.log_error_count = 0
        self.log_summary_var.set("Warnings: 0  Errors: 0")

    def reload_log_file(self) -> None:
        path = resolve_path(self.log_file_var.get(), DEFAULT_LOG_FILE)
        if not path.exists():
            self._replace_text(self.log_text, f"Лог-файл пока не найден:\n{path}")
            return
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            self._replace_text(self.log_text, f"Не удалось прочитать лог-файл:\n{exc}")
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_warning_count = 0
        self.log_error_count = 0
        for line in content.splitlines(keepends=True):
            tag = self._tag_for_log_line(line)
            if tag == "warn":
                self.log_warning_count += 1
            elif tag == "error":
                self.log_error_count += 1
            if tag:
                self.log_text.insert("end", line, tag)
            else:
                self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.log_summary_var.set(f"Warnings: {self.log_warning_count}  Errors: {self.log_error_count}")

    def _poll_process(self, process: subprocess.Popen[str]) -> None:
        if process is not self.process:
            return
        code = process.poll()
        if code is None:
            self.after(500, lambda proc=process: self._poll_process(proc))
            return
        if code == 0:
            self.status_var.set("Готово")
            self.status_pill.configure(bg="#10251c", fg=ACCENT_2)
            self.append_log(f"\n--- Процесс завершился успешно, code={code} ---\n", tag="ok")
        else:
            self.status_var.set(f"Завершено с ошибкой: {code}")
            self.status_pill.configure(bg="#2a1216", fg="#fca5a5")
            self.append_log(f"\n--- Процесс завершился, code={code} ---\n", tag="error")
        self.refresh_all()

    def _kill_if_alive(self, process: subprocess.Popen[str]) -> None:
        if process is self.process and process.poll() is None:
            process.kill()
            self.append_log("--- Процесс принудительно остановлен ---\n", tag="warn")

    def refresh_all(self) -> None:
        self.refresh_stats()
        self.refresh_overview()
        self.refresh_results()
        self.refresh_report()
        self.refresh_data()

    def _periodic_refresh(self) -> None:
        self.refresh_stats()
        if self.process and self.process.poll() is None:
            self.refresh_overview()
            self.refresh_results()
        self.after(10000, self._periodic_refresh)

    def refresh_stats(self) -> None:
        db_path = resolve_path(self.db_var.get(), "startups.db")
        if not db_path.exists():
            self.db_state_var.set(f"SQLite DB не найдена: {db_path.name}")
            self.total_profiles_var.set("0")
            self.startups_var.set("0")
            self.ai_due_var.set("0")
            self.filtered_var.set("0")
            self.tweets_var.set("0")
            self.mentions_var.set("0")
            self.last_update_var.set("нет данных")
            return

        try:
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                total = self._scalar(cur, "SELECT COUNT(*) FROM startups")
                startups = self._scalar(cur, "SELECT COUNT(*) FROM startups WHERE is_valuable = 1")
                ai_due = self._scalar(cur, "SELECT COUNT(*) FROM startups WHERE ai_due = 1")
                filtered = self._scalar(cur, "SELECT COUNT(*) FROM startups WHERE pre_filtered = 1")
                tweets = self._scalar(cur, "SELECT COUNT(*) FROM tweets")
                mentions = self._scalar(cur, "SELECT COUNT(*) FROM mentions")
                last_update = self._scalar(cur, "SELECT MAX(parsed_at) FROM startups") or "нет данных"
        except sqlite3.Error as exc:
            self.db_state_var.set(f"Ошибка чтения SQLite: {exc}")
            return

        self.db_state_var.set(f"SQLite DB: {db_path.name}")
        self.total_profiles_var.set(str(total))
        self.startups_var.set(str(startups))
        self.ai_due_var.set(str(ai_due))
        self.filtered_var.set(str(filtered))
        self.tweets_var.set(str(tweets))
        self.mentions_var.set(str(mentions))
        self.last_update_var.set(str(last_update)[:19])

    def refresh_overview(self) -> None:
        db_path = resolve_path(self.db_var.get(), "startups.db")
        if not db_path.exists():
            self._replace_text(self.overview_text, f"База пока не найдена:\n{db_path}")
            return

        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                total = self._scalar(cur, "SELECT COUNT(*) FROM startups")
                valuable = self._scalar(cur, "SELECT COUNT(*) FROM startups WHERE is_valuable = 1")
                skipped = self._scalar(cur, "SELECT COUNT(*) FROM startups WHERE is_valuable = 0 AND COALESCE(pre_filtered, 0) = 0")
                filtered = self._scalar(cur, "SELECT COUNT(*) FROM startups WHERE pre_filtered = 1")
                ai_due = self._scalar(cur, "SELECT COUNT(*) FROM startups WHERE ai_due = 1")
                without_ai = self._scalar(cur, "SELECT COUNT(*) FROM startups WHERE is_valuable IS NULL")
                tweets = self._scalar(cur, "SELECT COUNT(*) FROM tweets")
                mentions = self._scalar(cur, "SELECT COUNT(*) FROM mentions")
                last_update = self._scalar(cur, "SELECT MAX(parsed_at) FROM startups") or "нет данных"

                category_rows = cur.execute(
                    """
                    SELECT COALESCE(NULLIF(category, ''), 'Без категории') AS name, COUNT(*) AS total
                    FROM startups
                    GROUP BY name
                    ORDER BY total DESC, name
                    LIMIT 20
                    """
                ).fetchall()
                depth_rows = cur.execute(
                    """
                    SELECT COALESCE(CAST(depth AS TEXT), 'unknown') AS name, COUNT(*) AS total
                    FROM startups
                    GROUP BY name
                    ORDER BY name
                    """
                ).fetchall()
                filter_rows = cur.execute(
                    """
                    SELECT COALESCE(NULLIF(filter_reason, ''), 'Без причины') AS name, COUNT(*) AS total
                    FROM startups
                    WHERE pre_filtered = 1
                    GROUP BY name
                    ORDER BY total DESC, name
                    LIMIT 15
                    """
                ).fetchall()
                latest_rows = cur.execute(
                    """
                    SELECT username, display_username, category, is_valuable, pre_filtered, ai_due, parsed_at
                    FROM startups
                    ORDER BY parsed_at DESC
                    LIMIT 12
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            self._replace_text(self.overview_text, f"Ошибка чтения SQLite:\n{exc}")
            return

        lines = [
            "Сводка парсинга",
            f"База: {db_path}",
            f"Последняя запись: {str(last_update)[:19]}",
            "",
            "Состояния",
            f"Всего профилей: {total}",
            f"Стартапы: {valuable}",
            f"Не стартапы: {skipped}",
            f"Отфильтровано до AI: {filtered}",
            f"Ожидают повторного AI: {ai_due}",
            f"Без AI-вердикта: {without_ai}",
            f"Твиты в базе: {tweets}",
            f"Упоминания в базе: {mentions}",
            "",
            "Категории",
        ]
        lines.extend(self._format_count_rows(category_rows))
        lines.extend(["", "Глубины"])
        lines.extend(self._format_count_rows(depth_rows, prefix="Depth "))
        lines.extend(["", "Причины pre-filter"])
        lines.extend(self._format_count_rows(filter_rows))
        lines.extend(["", "Последние профили"])
        if latest_rows:
            for row in latest_rows:
                details = dict(row)
                username = details.get("display_username") or details.get("username")
                lines.append(
                    f"@{username}  |  {self._profile_status(details)}  |  "
                    f"{details.get('category') or 'Без категории'}  |  {(details.get('parsed_at') or '')[:19]}"
                )
        else:
            lines.append("Нет записей.")

        self._render_overview("\n".join(lines))

    def _format_count_rows(self, rows, prefix: str = "") -> list[str]:
        if not rows:
            return ["Нет данных."]
        return [f"{prefix}{row['name']}: {row['total']}" for row in rows]

    def refresh_results(self) -> None:
        db_path = resolve_path(self.db_var.get(), "startups.db")
        self.results_tree.delete(*self.results_tree.get_children())
        self.details_by_iid.clear()
        self._replace_text(self.detail_text, "Выберите проект в таблице.")

        if not db_path.exists():
            return

        scope = self.scope_var.get()
        category = self.category_var.get()
        search = self.search_var.get().strip()
        where: list[str] = []
        params: list[str] = []

        if scope == "Стартапы":
            where.append("is_valuable = 1")
        elif scope == "AI pending":
            where.append("ai_due = 1")
        elif scope == "Отфильтрованные":
            where.append("pre_filtered = 1")
        elif scope == "Не стартапы":
            where.append("is_valuable = 0 AND COALESCE(pre_filtered, 0) = 0")
        elif scope == "Без AI-вердикта":
            where.append("is_valuable IS NULL")

        if category and category != "Все":
            where.append("category = ?")
            params.append(category)
        if search:
            where.append(
                "(username LIKE ? OR display_username LIKE ? OR bio LIKE ? OR pitch LIKE ? OR red_flags LIKE ? OR filter_reason LIKE ?)"
            )
            needle = f"%{search}%"
            params.extend([needle, needle, needle, needle, needle, needle])

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        query = f"""
            SELECT s.username, s.display_username, s.url, s.bio, s.depth, s.is_valuable,
                   s.category, s.stage, s.pitch, s.red_flags, s.pre_filtered,
                   s.filter_reason, s.parsed_at, s.ai_due,
                   (SELECT COUNT(*) FROM tweets WHERE username = s.username) AS tweet_count,
                   (SELECT COUNT(*) FROM mentions WHERE username = s.username) AS mention_count
            FROM startups s
            {where_sql}
            ORDER BY s.parsed_at DESC
        """

        try:
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                rows = cur.execute(query, params).fetchall()
                for row in rows:
                    username = row["username"]
                    tweets = [
                        item["tweet"]
                        for item in cur.execute(
                            "SELECT tweet FROM tweets WHERE username = ? ORDER BY id",
                            (username,),
                        ).fetchall()
                    ]
                    mentions = [
                        item["mention"]
                        for item in cur.execute(
                            "SELECT mention FROM mentions WHERE username = ? ORDER BY id",
                            (username,),
                        ).fetchall()
                    ]
                    details = dict(row)
                    details["tweets"] = tweets
                    details["mentions"] = mentions
                    status = self._profile_status(details)
                    iid = self.results_tree.insert(
                        "",
                        "end",
                        values=(
                            status,
                            f"@{row['display_username'] or username}",
                            row["category"] or "Other",
                            row["stage"] or "unknown",
                            row["depth"] if row["depth"] is not None else "",
                            row["tweet_count"],
                            row["mention_count"],
                            (row["parsed_at"] or "")[:19],
                        ),
                    )
                    self.details_by_iid[iid] = details
        except sqlite3.Error as exc:
            self._replace_text(self.detail_text, f"Ошибка чтения SQLite:\n{exc}")

    def _update_report_combo(self) -> None:
        main_report_path = resolve_path(self.report_var.get(), "report.md")
        reports_dir = main_report_path.parent / "reports"
        
        values = ["Текущий"]
        if reports_dir.exists() and reports_dir.is_dir():
            files = sorted([f.name for f in reports_dir.glob("*.md")], reverse=True)
            for f in files:
                values.append(f"История: {f}")
        
        if hasattr(self, 'report_combo'):
            self.report_combo['values'] = values
            if self.view_report_var.get() not in values:
                self.view_report_var.set("Текущий")

    def get_selected_report_path(self) -> Path:
        selected = self.view_report_var.get()
        main_report_path = resolve_path(self.report_var.get(), "report.md")
        if selected.startswith("История: "):
            fname = selected.replace("История: ", "")
            return main_report_path.parent / "reports" / fname
        return main_report_path

    def refresh_report(self) -> None:
        self._update_report_combo()
        report_path = self.get_selected_report_path()
        if not report_path.exists():
            self._replace_text(self.report_text, f"Отчет пока не найден:\n{report_path}")
            return
        try:
            content = report_path.read_text(encoding="utf-8")
        except OSError as exc:
            content = f"Не удалось прочитать отчет:\n{exc}"
            
        raw_mode = self.report_raw_var.get()
        if hasattr(self, '_last_report_content') and content == self._last_report_content and raw_mode == self._last_report_raw:
            return  # No changes, avoid expensive re-render
            
        self._last_report_content = content
        self._last_report_raw = raw_mode

        if raw_mode:
            self._replace_text(self.report_text, content)
        else:
            self._render_report(content)

    def refresh_data(self) -> None:
        db_path = resolve_path(self.db_var.get(), "startups.db")
        if not db_path.exists():
            self.last_raw_rows = []
            self._replace_text(self.data_text, f"База пока не найдена:\n{db_path}")
            return

        try:
            rows = self._load_raw_rows(db_path)
        except sqlite3.Error as exc:
            self.last_raw_rows = []
            self._replace_text(self.data_text, f"Ошибка чтения SQLite:\n{exc}")
            return

        self.last_raw_rows = rows
        payload = {
            "database": str(db_path),
            "profiles": rows,
        }
        self._replace_text(self.data_text, json.dumps(payload, ensure_ascii=False, indent=2))

    def _load_raw_rows(self, db_path: Path) -> list[dict]:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            rows = cur.execute(
                """
                SELECT username, display_username, url, bio, depth, is_valuable, category,
                       stage, pitch, red_flags, pre_filtered, filter_reason, parsed_at, ai_due
                FROM startups
                ORDER BY parsed_at DESC
                """
            ).fetchall()
            result: list[dict] = []
            for row in rows:
                username = row["username"]
                item = dict(row)
                item["status"] = self._profile_status(item)
                item["tweets"] = [
                    tweet_row["tweet"]
                    for tweet_row in cur.execute(
                        "SELECT tweet FROM tweets WHERE username = ? ORDER BY id",
                        (username,),
                    ).fetchall()
                ]
                item["mentions"] = [
                    mention_row["mention"]
                    for mention_row in cur.execute(
                        "SELECT mention FROM mentions WHERE username = ? ORDER BY id",
                        (username,),
                    ).fetchall()
                ]
                result.append(item)
            return result

    def export_data_json(self) -> None:
        if not self.last_raw_rows:
            self.refresh_data()
        selected = filedialog.asksaveasfilename(
            initialdir=ROOT,
            initialfile="crawler-data.json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not selected:
            return
        payload = {
            "database": str(resolve_path(self.db_var.get(), "startups.db")),
            "profiles": self.last_raw_rows,
        }
        try:
            Path(selected).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Не удалось сохранить JSON", str(exc))

    def _scalar(self, cur: sqlite3.Cursor, query: str):
        try:
            cur.execute(query)
            row = cur.fetchone()
            return row[0] if row else 0
        except sqlite3.Error:
            return 0

    def _on_result_selected(self, _event=None) -> None:
        selection = self.results_tree.selection()
        if not selection:
            return
        details = self.details_by_iid.get(selection[0])
        if not details:
            return
        username = details.get("display_username") or details.get("username")
        lines = [
            f"@{username}",
            "",
            f"Статус: {self._profile_status(details)}",
            f"Категория: {details.get('category') or 'Other'}",
            f"Стадия: {details.get('stage') or 'unknown'}",
            f"Depth: {details.get('depth')}",
            f"AI pending: {'да' if details.get('ai_due') else 'нет'}",
            f"Pre-filtered: {'да' if details.get('pre_filtered') else 'нет'}",
            f"Причина фильтра: {details.get('filter_reason') or 'нет'}",
            f"Дата: {(details.get('parsed_at') or '')[:19]}",
            "",
            "Ссылка:",
            details.get("url") or f"https://x.com/{details.get('username')}",
            "",
        ]
        if details.get("bio"):
            lines.extend(["Bio:", details["bio"], ""])
        if details.get("pitch"):
            lines.extend(["Почему интересно:", details["pitch"], ""])
        if details.get("red_flags"):
            lines.extend(["Риски:", details["red_flags"], ""])
        mentions = details.get("mentions") or []
        if mentions:
            lines.append("Найденные упоминания:")
            lines.append(", ".join(f"@{mention}" for mention in mentions))
            lines.append("")
        tweets = details.get("tweets") or []
        if tweets:
            lines.append(f"Твиты ({len(tweets)}):")
            for index, tweet in enumerate(tweets, start=1):
                lines.append(f"{index}. {tweet}")
                lines.append("")
        self._replace_text(self.detail_text, "\n".join(lines).strip())

    def _profile_status(self, details: dict) -> str:
        if details.get("ai_due"):
            return "AI pending"
        if details.get("pre_filtered"):
            return "Filtered"
        is_valuable = details.get("is_valuable")
        if is_valuable in (1, True):
            return "Startup"
        if is_valuable in (0, False) and is_valuable is not None:
            return "Skip"
        return "Parsed"

    def open_selected_url(self) -> None:
        selection = self.results_tree.selection()
        if not selection:
            return
        details = self.details_by_iid.get(selection[0], {})
        url = details.get("url") or f"https://x.com/{details.get('username', '')}"
        if url:
            webbrowser.open(url)

    def open_report_file(self) -> None:
        self._open_path(self.get_selected_report_path())

    def open_db_file(self) -> None:
        self._open_path(resolve_path(self.db_var.get(), "startups.db"))

    def open_log_file(self) -> None:
        path = resolve_path(self.log_file_var.get(), DEFAULT_LOG_FILE)
        if not path.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Не удалось создать лог-файл", str(exc))
                return
        self._open_path(path)

    def _open_path(self, path: Path) -> None:
        if not path.exists():
            messagebox.showwarning("Файл не найден", str(path))
            return
        try:
            if os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            messagebox.showerror("Не удалось открыть файл", str(exc))

    def _render_overview(self, content: str) -> None:
        self.overview_text.configure(state="normal")
        self.overview_text.delete("1.0", "end")
        section_names = {
            "Сводка парсинга",
            "Состояния",
            "Категории",
            "Глубины",
            "Причины pre-filter",
            "Последние профили",
        }
        for line in content.splitlines():
            tag = "section" if line in section_names else "muted" if line.startswith("База:") else None
            if tag:
                self.overview_text.insert("end", line + "\n", tag)
            else:
                self.overview_text.insert("end", line + "\n")
        self.overview_text.configure(state="disabled")

    def _render_report(self, content: str) -> None:
        self.report_text.configure(state="normal")
        self.report_text.delete("1.0", "end")
        for raw_line in content.splitlines():
            line = raw_line.rstrip()
            if line.startswith("# "):
                self.report_text.insert("end", line[2:] + "\n", "h1")
            elif line.startswith("## "):
                self.report_text.insert("end", line[3:] + "\n", "h2")
            elif line.startswith("### "):
                self.report_text.insert("end", line[4:] + "\n", "h3")
            elif line.startswith(">"):
                self.report_text.insert("end", line.lstrip("> ").strip() + "\n", "quote")
            elif line == "---":
                self.report_text.insert("end", "────────────────────────────────────────\n", "separator")
            elif line.startswith("**") or "**" in line:
                pretty = line.replace("**", "")
                self.report_text.insert("end", pretty + "\n", "meta")
            else:
                self.report_text.insert("end", line + "\n")
        self.report_text.configure(state="disabled")

    def _replace_text(self, widget: tk.Text, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _on_close(self) -> None:
        if self.process and self.process.poll() is None:
            if not messagebox.askyesno("Процесс работает", "Остановить краулер и закрыть GUI?"):
                return
            self.stop_crawler()
        self.destroy()


if __name__ == "__main__":
    app = RadarGUI()
    app.mainloop()
