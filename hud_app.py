"""
hud_app.py - Panel de Control Oficial & Gamer HUD para Bluetti Elite 100 V2 (Windows 10/11)
============================================================================================
Diseño moderno de alta tecnología (Cyber-Dark / Glassmorphism) con:
  1. Panel Principal Completo con medidores, flujo de energía en tiempo real y switches Modbus.
  2. Modo HUD Gamer Flotante Ultracompacto, translúcido, sin marcos y Always-on-Top.
  3. Motor de Criptografía Oficial ECDH SECP256R1 + AES-CBC (conexión 100% estable sin caídas).
  4. Selector de velocidad de carga AC, modo ECO y Power Lifting (2700W).
"""

import sys
import tkinter as tk
from typing import Dict, Any, List
import customtkinter as ctk
from ble_manager import BluettiBleManager

# Tema visual oscuro moderno
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


class BluettiDesktopApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Bluetti Elite 100 V2 • Centro de Control & Gamer HUD")
        self.geometry("1000x740")
        self.minsize(880, 640)
        self.configure(fg_color="#080b11")

        # Variables de estado y arrastre de ventana
        self.is_gamer_hud_mode = False
        self._drag_start_x = 0
        self._drag_start_y = 0
        self.discovered_devices: List[Dict[str, str]] = []

        # Instancia del Gestor BLE Oficial con Criptografía ECDH
        self.ble = BluettiBleManager(
            on_telemetry_callback=self._handle_telemetry,
            on_status_callback=self._handle_status,
            on_devices_discovered=self._handle_devices_discovered,
            poll_interval=1.5
        )

        # Construir Interfaz Gráfica
        self._build_app_structure()

        # Iniciar servicio BLE
        self.ble.start()

    def _build_app_structure(self):
        """Contenedor raíz para alternar entre Panel Completo y HUD Gamer."""
        self.main_container = ctk.CTkFrame(self, fg_color="transparent")
        self.main_container.pack(fill="both", expand=True)

        self.hud_container = ctk.CTkFrame(self, fg_color="#0a0e17", corner_radius=16, border_width=1, border_color="#00f0ff")

        self._build_full_dashboard()
        self._build_gamer_hud()

        # Mostrar por defecto el Dashboard Completo
        self._show_full_dashboard()

    # =========================================================================
    # VISTA 1: DASHBOARD COMPLETO (DISEÑO HIGH-TECH)
    # =========================================================================
    def _build_full_dashboard(self):
        f = self.main_container

        # 1. Barra de Navegación Superior
        top_bar = ctk.CTkFrame(f, fg_color="#0e141f", height=62, corner_radius=0, border_width=1, border_color="#1e293b")
        top_bar.pack(fill="x", side="top", padx=0, pady=0)

        # Título y Modelo
        title_box = ctk.CTkFrame(top_bar, fg_color="transparent")
        title_box.pack(side="left", padx=18, pady=8)

        lbl_app = ctk.CTkLabel(
            title_box, text="⚡ BLUETTI ELITE 100 V2",
            font=("Segoe UI", 16, "bold"), text_color="#00f0ff"
        )
        lbl_app.pack(anchor="w")

        lbl_sub = ctk.CTkLabel(
            title_box, text="Centro de Control Oficial • Cifrado ECDH + AES-CBC • 1024Wh LiFePO4",
            font=("Segoe UI", 10), text_color="#94a3b8"
        )
        lbl_sub.pack(anchor="w")

        # Botones de Acción Superior
        actions_box = ctk.CTkFrame(top_bar, fg_color="transparent")
        actions_box.pack(side="right", padx=14, pady=8)

        # Botón para cambiar al HUD Gamer
        self.btn_switch_hud = ctk.CTkButton(
            actions_box, text="🎮 HUD Gamer Flotante",
            font=("Segoe UI", 11, "bold"), width=160, height=36,
            fg_color="#1d4ed8", hover_color="#2563eb", text_color="#ffffff",
            corner_radius=10, command=self._switch_to_gamer_hud
        )
        self.btn_switch_hud.pack(side="right", padx=6)

        # Botón Modo Simulación
        self.btn_sim_mode = ctk.CTkButton(
            actions_box, text="🧪 Modo Demo / Simular",
            font=("Segoe UI", 11, "bold"), width=150, height=36,
            fg_color="#065f46", hover_color="#047857", text_color="#34d399",
            corner_radius=10, command=self._toggle_simulation
        )
        self.btn_sim_mode.pack(side="right", padx=6)

        # Botón Liberar BLE para el móvil
        self.btn_release_ble = ctk.CTkButton(
            actions_box, text="⏸ Liberar BLE (Móvil)",
            font=("Segoe UI", 11), width=145, height=36,
            fg_color="#1e293b", hover_color="#334155", text_color="#cbd5e1",
            corner_radius=10, command=self._toggle_ble_release
        )
        self.btn_release_ble.pack(side="right", padx=6)

        # 2. Barra de Estado de Conexión & Selector Bluetooth
        status_bar = ctk.CTkFrame(f, fg_color="#0b0f17", height=42, corner_radius=0)
        status_bar.pack(fill="x", padx=16, pady=(10, 0))

        self.lbl_status = ctk.CTkLabel(
            status_bar, text="◌ Negociando Enlace Seguro ECDH...",
            font=("Segoe UI", 11, "bold"), text_color="#fbbf24"
        )
        self.lbl_status.pack(side="left", padx=12, pady=6)

        # Selector de Dispositivo BLE
        dev_picker_box = ctk.CTkFrame(status_bar, fg_color="transparent")
        dev_picker_box.pack(side="right", padx=12, pady=4)

        lbl_pick = ctk.CTkLabel(dev_picker_box, text="Dispositivo:", font=("Segoe UI", 10), text_color="#94a3b8")
        lbl_pick.pack(side="left", padx=4)

        self.combo_devices = ctk.CTkComboBox(
            dev_picker_box, values=["DC:B4:D9:54:78:1A (Por Defecto)"], width=240, height=28,
            font=("Segoe UI", 10), command=self._on_device_selected
        )
        self.combo_devices.pack(side="left", padx=4)

        # 3. Contenido Principal Scrollable
        content_scroll = ctk.CTkScrollableFrame(f, fg_color="transparent")
        content_scroll.pack(fill="both", expand=True, padx=16, pady=10)

        # -------------------------------------------------------------
        # SECCIÓN A: DIAGRAMA DE FLUJO DE ENERGÍA (3 COLUMNAS)
        # -------------------------------------------------------------
        flow_card = ctk.CTkFrame(content_scroll, fg_color="#0e141f", corner_radius=16, border_width=1, border_color="#1e293b")
        flow_card.pack(fill="x", pady=(0, 12), padx=0, ipady=8)

        flow_title = ctk.CTkLabel(
            flow_card, text="⚡ FLUJO DE ENERGÍA EN TIEMPO REAL (ENTRADAS • BATERÍA • SALIDAS)",
            font=("Segoe UI", 11, "bold"), text_color="#00f0ff"
        )
        flow_title.pack(anchor="w", padx=16, pady=(10, 6))

        grid_3col = ctk.CTkFrame(flow_card, fg_color="transparent")
        grid_3col.pack(fill="x", padx=12, pady=4)
        grid_3col.columnconfigure(0, weight=1)
        grid_3col.columnconfigure(1, weight=1)
        grid_3col.columnconfigure(2, weight=1)

        # Columna 1: ENTRADAS (Solar MPPT + Red AC)
        col_inputs = ctk.CTkFrame(grid_3col, fg_color="#080b11", corner_radius=14, border_width=1, border_color="#1e293b")
        col_inputs.grid(row=0, column=0, padx=6, pady=4, sticky="nsew")

        ctk.CTkLabel(col_inputs, text="ENTRADA TOTAL", font=("Segoe UI", 10, "bold"), text_color="#94a3b8").pack(anchor="w", padx=14, pady=(10, 2))
        self.lbl_in_total_w = ctk.CTkLabel(col_inputs, text="0 W", font=("Segoe UI", 26, "bold"), text_color="#00f0ff")
        self.lbl_in_total_w.pack(anchor="w", padx=14)

        # Sub-tarjeta Solar
        sub_solar = ctk.CTkFrame(col_inputs, fg_color="#0e141f", corner_radius=10, border_width=1, border_color="#332a10")
        sub_solar.pack(fill="x", padx=10, pady=6, ipady=3)
        ctk.CTkLabel(sub_solar, text="☀️ Solar MPPT (Max 1000W)", font=("Segoe UI", 10, "bold"), text_color="#fbbf24").pack(anchor="w", padx=10, pady=(4, 0))
        self.lbl_solar_val = ctk.CTkLabel(sub_solar, text="0 W", font=("Segoe UI", 16, "bold"), text_color="#fde047")
        self.lbl_solar_val.pack(anchor="w", padx=10)
        self.lbl_solar_sub = ctk.CTkLabel(sub_solar, text="0.0V • 0.0A", font=("Consolas", 10), text_color="#94a3b8")
        self.lbl_solar_sub.pack(anchor="w", padx=10, pady=(0, 4))

        # Sub-tarjeta Red Eléctrica AC
        sub_ac_in = ctk.CTkFrame(col_inputs, fg_color="#0e141f", corner_radius=10, border_width=1, border_color="#102035")
        sub_ac_in.pack(fill="x", padx=10, pady=(0, 10), ipady=3)
        ctk.CTkLabel(sub_ac_in, text="🔌 Red AC (Max 1200W)", font=("Segoe UI", 10, "bold"), text_color="#38bdf8").pack(anchor="w", padx=10, pady=(4, 0))
        self.lbl_ac_in_val = ctk.CTkLabel(sub_ac_in, text="0 W", font=("Segoe UI", 16, "bold"), text_color="#60a5fa")
        self.lbl_ac_in_val.pack(anchor="w", padx=10)
        self.lbl_ac_in_sub = ctk.CTkLabel(sub_ac_in, text="120.0V • 0.0A", font=("Consolas", 10), text_color="#94a3b8")
        self.lbl_ac_in_sub.pack(anchor="w", padx=10, pady=(0, 4))

        # Columna 2: BATERÍA CENTRAL
        col_batt = ctk.CTkFrame(grid_3col, fg_color="#080b11", corner_radius=14, border_width=1, border_color="#1e293b")
        col_batt.grid(row=0, column=1, padx=6, pady=4, sticky="nsew")

        ctk.CTkLabel(col_batt, text="BATERÍA LiFePO4", font=("Segoe UI", 10, "bold"), text_color="#94a3b8").pack(anchor="w", padx=14, pady=(10, 2))
        self.lbl_soc_big = ctk.CTkLabel(col_batt, text="84%", font=("Segoe UI", 46, "bold"), text_color="#00ff88")
        self.lbl_soc_big.pack(pady=(2, 0))

        self.progress_soc_big = ctk.CTkProgressBar(col_batt, height=12, corner_radius=6, progress_color="#00ff88", fg_color="#1e293b")
        self.progress_soc_big.pack(fill="x", padx=20, pady=(6, 8))
        self.progress_soc_big.set(0.84)

        self.lbl_wh_estimate = ctk.CTkLabel(col_batt, text="860 / 1024 Wh Disponibles", font=("Consolas", 11, "bold"), text_color="#cbd5e1")
        self.lbl_wh_estimate.pack()

        self.lbl_net_power = ctk.CTkLabel(col_batt, text="Flujo Neto: 0 W", font=("Segoe UI", 11, "bold"), text_color="#94a3b8")
        self.lbl_net_power.pack(pady=4)

        self.lbl_batt_volts = ctk.CTkLabel(col_batt, text="Tensión de Celda: 51.2 V", font=("Segoe UI", 10), text_color="#38bdf8")
        self.lbl_batt_volts.pack(pady=(0, 10))

        # Columna 3: SALIDAS (Inversor AC + Salidas DC)
        col_outputs = ctk.CTkFrame(grid_3col, fg_color="#080b11", corner_radius=14, border_width=1, border_color="#1e293b")
        col_outputs.grid(row=0, column=2, padx=6, pady=4, sticky="nsew")

        ctk.CTkLabel(col_outputs, text="SALIDA TOTAL", font=("Segoe UI", 10, "bold"), text_color="#94a3b8").pack(anchor="w", padx=14, pady=(10, 2))
        self.lbl_out_total_w = ctk.CTkLabel(col_outputs, text="0 W", font=("Segoe UI", 26, "bold"), text_color="#ff3366")
        self.lbl_out_total_w.pack(anchor="w", padx=14)

        # Sub-tarjeta Tomas AC
        sub_ac_out = ctk.CTkFrame(col_outputs, fg_color="#0e141f", corner_radius=10, border_width=1, border_color="#35101a")
        sub_ac_out.pack(fill="x", padx=10, pady=6, ipady=3)
        ctk.CTkLabel(sub_ac_out, text="⚡ Inversor AC (1800W)", font=("Segoe UI", 10, "bold"), text_color="#f87171").pack(anchor="w", padx=10, pady=(4, 0))
        self.lbl_ac_out_val = ctk.CTkLabel(sub_ac_out, text="0 W", font=("Segoe UI", 16, "bold"), text_color="#fb7185")
        self.lbl_ac_out_val.pack(anchor="w", padx=10)
        ctk.CTkLabel(sub_ac_out, text="120V • Pura Onda Sinusoidal", font=("Consolas", 10), text_color="#94a3b8").pack(anchor="w", padx=10, pady=(0, 4))

        # Sub-tarjeta Salidas DC
        sub_dc_out = ctk.CTkFrame(col_outputs, fg_color="#0e141f", corner_radius=10, border_width=1, border_color="#2d1035")
        sub_dc_out.pack(fill="x", padx=10, pady=(0, 10), ipady=3)
        ctk.CTkLabel(sub_dc_out, text="🔋 Salidas DC (USB/12V)", font=("Segoe UI", 10, "bold"), text_color="#c084fc").pack(anchor="w", padx=10, pady=(4, 0))
        self.lbl_dc_out_val = ctk.CTkLabel(sub_dc_out, text="0 W", font=("Segoe UI", 16, "bold"), text_color="#d8b4fe")
        self.lbl_dc_out_val.pack(anchor="w", padx=10)
        ctk.CTkLabel(sub_dc_out, text="USB-C 140W+100W • 12V 10A", font=("Consolas", 10), text_color="#94a3b8").pack(anchor="w", padx=10, pady=(0, 4))

        # -------------------------------------------------------------
        # SECCIÓN B: PANEL DE CONMUTADORES DE HARDWARE MODBUS
        # -------------------------------------------------------------
        ctrl_card = ctk.CTkFrame(content_scroll, fg_color="#0e141f", corner_radius=16, border_width=1, border_color="#1e293b")
        ctrl_card.pack(fill="x", pady=(0, 12), padx=0, ipady=8)

        ctk.CTkLabel(
            ctrl_card, text="⚙️ CONMUTADORES MODBUS RTU (COMANDOS CRIPTOGRÁFICOS)",
            font=("Segoe UI", 11, "bold"), text_color="#00f0ff"
        ).pack(anchor="w", padx=16, pady=(10, 8))

        switches_grid = ctk.CTkFrame(ctrl_card, fg_color="transparent")
        switches_grid.pack(fill="x", padx=16, pady=4)
        switches_grid.columnconfigure(0, weight=1)
        switches_grid.columnconfigure(1, weight=1)
        switches_grid.columnconfigure(2, weight=1)
        switches_grid.columnconfigure(3, weight=1)
        switches_grid.columnconfigure(4, weight=1)

        # 1. Switch AC
        f_sw_ac = ctk.CTkFrame(switches_grid, fg_color="#080b11", corner_radius=12, border_width=1, border_color="#1e293b")
        f_sw_ac.grid(row=0, column=0, padx=4, pady=4, sticky="nsew", ipady=6)
        ctk.CTkLabel(f_sw_ac, text="Salida CA (1800W)", font=("Segoe UI", 11, "bold"), text_color="#ffffff").pack(pady=(6, 2))
        self.sw_ac = ctk.CTkSwitch(f_sw_ac, text="", command=self._on_toggle_ac, progress_color="#ff3366")
        self.sw_ac.pack(pady=4)

        # 2. Switch DC
        f_sw_dc = ctk.CTkFrame(switches_grid, fg_color="#080b11", corner_radius=12, border_width=1, border_color="#1e293b")
        f_sw_dc.grid(row=0, column=1, padx=4, pady=4, sticky="nsew", ipady=6)
        ctk.CTkLabel(f_sw_dc, text="Salida CC (12V/USB)", font=("Segoe UI", 11, "bold"), text_color="#ffffff").pack(pady=(6, 2))
        self.sw_dc = ctk.CTkSwitch(f_sw_dc, text="", command=self._on_toggle_dc, progress_color="#00f0ff")
        self.sw_dc.pack(pady=4)

        # 3. Switch Modo ECO
        f_sw_eco = ctk.CTkFrame(switches_grid, fg_color="#080b11", corner_radius=12, border_width=1, border_color="#1e293b")
        f_sw_eco.grid(row=0, column=2, padx=4, pady=4, sticky="nsew", ipady=6)
        ctk.CTkLabel(f_sw_eco, text="Modo ECO", font=("Segoe UI", 11, "bold"), text_color="#ffffff").pack(pady=(6, 2))
        self.sw_eco = ctk.CTkSwitch(f_sw_eco, text="", command=self._on_toggle_eco, progress_color="#00ff88")
        self.sw_eco.pack(pady=4)
        self.sw_eco.select()

        # 4. Switch Refuerzo de Red (Grid Self-Adaptation)
        f_sw_grid = ctk.CTkFrame(switches_grid, fg_color="#080b11", corner_radius=12, border_width=1, border_color="#1e293b")
        f_sw_grid.grid(row=0, column=3, padx=4, pady=4, sticky="nsew", ipady=6)
        ctk.CTkLabel(f_sw_grid, text="Refuerzo Red", font=("Segoe UI", 11, "bold"), text_color="#ffffff").pack(pady=(6, 2))
        self.sw_grid = ctk.CTkSwitch(f_sw_grid, text="", command=self._on_toggle_grid, progress_color="#f59e0b")
        self.sw_grid.pack(pady=4)

        # 5. Switch Power Lifting
        f_sw_pl = ctk.CTkFrame(switches_grid, fg_color="#080b11", corner_radius=12, border_width=1, border_color="#1e293b")
        f_sw_pl.grid(row=0, column=4, padx=4, pady=4, sticky="nsew", ipady=6)
        ctk.CTkLabel(f_sw_pl, text="Power Lifting (2700W)", font=("Segoe UI", 11, "bold"), text_color="#ffffff").pack(pady=(6, 2))
        self.sw_pl = ctk.CTkSwitch(f_sw_pl, text="", command=self._on_toggle_pl, progress_color="#a855f7")
        self.sw_pl.pack(pady=4)

        # Selector de Velocidad de Carga AC (coincidiendo exactamente con la app oficial)
        charge_frame = ctk.CTkFrame(ctrl_card, fg_color="#080b11", corner_radius=12, border_width=1, border_color="#1e293b")
        charge_frame.pack(fill="x", padx=16, pady=(10, 12), ipady=6)

        ctk.CTkLabel(charge_frame, text="⚡ MODO DE CARGA (RED AC):", font=("Segoe UI", 10, "bold"), text_color="#94a3b8").pack(side="left", padx=14)

        self.btn_charge_silent = ctk.CTkButton(
            charge_frame, text="Silencioso (≤300W)", width=135, height=32,
            fg_color="#1e293b", hover_color="#334155", font=("Segoe UI", 10),
            command=lambda: self._set_charge_mode("silent")
        )
        self.btn_charge_silent.pack(side="left", padx=6)

        self.btn_charge_standard = ctk.CTkButton(
            charge_frame, text="Estándar (~800W)", width=135, height=32,
            fg_color="#1d4ed8", hover_color="#2563eb", font=("Segoe UI", 10, "bold"),
            command=lambda: self._set_charge_mode("standard")
        )
        self.btn_charge_standard.pack(side="left", padx=6)

        self.btn_charge_turbo = ctk.CTkButton(
            charge_frame, text="Carga Rápida / Turbo (1200W)", width=195, height=32,
            fg_color="#1e293b", hover_color="#334155", font=("Segoe UI", 10),
            command=lambda: self._set_charge_mode("turbo")
        )
        self.btn_charge_turbo.pack(side="left", padx=6)

    # =========================================================================
    # VISTA 2: MODO HUD GAMER FLOTANTE COMPACTO
    # =========================================================================
    def _build_gamer_hud(self):
        h = self.hud_container

        # 1. Barra de Título Arrastrable
        h_title_bar = ctk.CTkFrame(h, fg_color="#0e141f", corner_radius=14, height=38)
        h_title_bar.pack(fill="x", side="top", padx=4, pady=4)
        h_title_bar.bind("<ButtonPress-1>", self._start_drag)
        h_title_bar.bind("<B1-Motion>", self._do_drag)

        icon_lbl = ctk.CTkLabel(h_title_bar, text="⚡ BLUETTI HUD GAMER", font=("Segoe UI", 11, "bold"), text_color="#00f0ff")
        icon_lbl.pack(side="left", padx=10, pady=4)
        icon_lbl.bind("<ButtonPress-1>", self._start_drag)
        icon_lbl.bind("<B1-Motion>", self._do_drag)

        # Botón Volver
        btn_back = ctk.CTkButton(
            h_title_bar, text="✕ Pantalla Completa", width=130, height=24,
            fg_color="#1d4ed8", hover_color="#2563eb", text_color="#ffffff",
            font=("Segoe UI", 10, "bold"), corner_radius=8, command=self._switch_to_full_dashboard
        )
        btn_back.pack(side="right", padx=6, pady=4)

        # 2. Contenido HUD
        h_content = ctk.CTkFrame(h, fg_color="transparent")
        h_content.pack(fill="both", expand=True, padx=12, pady=6)

        # Tarjeta Batería HUD
        h_soc_card = ctk.CTkFrame(h_content, fg_color="#080b11", corner_radius=12, border_width=1, border_color="#1e293b")
        h_soc_card.pack(fill="x", pady=4, ipady=6)

        self.hud_soc_val = ctk.CTkLabel(h_soc_card, text="84%", font=("Segoe UI", 36, "bold"), text_color="#00ff88")
        self.hud_soc_val.pack()

        self.hud_progress_soc = ctk.CTkProgressBar(h_soc_card, height=10, corner_radius=5, progress_color="#00ff88", fg_color="#1e293b")
        self.hud_progress_soc.pack(fill="x", padx=20, pady=(2, 6))
        self.hud_progress_soc.set(0.84)

        self.hud_net_power = ctk.CTkLabel(h_soc_card, text="Flujo Neto: 0 W", font=("Segoe UI", 10, "bold"), text_color="#94a3b8")
        self.hud_net_power.pack()

        # Cuadrícula de Potencias HUD
        h_grid = ctk.CTkFrame(h_content, fg_color="transparent")
        h_grid.pack(fill="x", pady=6)
        h_grid.columnconfigure(0, weight=1)
        h_grid.columnconfigure(1, weight=1)

        self.hud_solar_w = self._create_hud_metric(h_grid, "SOLAR MPPT", "0 W", "38.4V", "#fbbf24", 0, 0)
        self.hud_grid_w = self._create_hud_metric(h_grid, "RED AC", "0 W", "120V", "#38bdf8", 0, 1)
        self.hud_ac_out_w = self._create_hud_metric(h_grid, "INVERSOR AC", "0 W", "Tomas 120V", "#ff3366", 1, 0)
        self.hud_dc_out_w = self._create_hud_metric(h_grid, "SALIDAS DC", "0 W", "USB-C/12V", "#c084fc", 1, 1)

        # Switches en HUD
        h_ctrl = ctk.CTkFrame(h_content, fg_color="#080b11", corner_radius=10, border_width=1, border_color="#1e293b")
        h_ctrl.pack(fill="x", pady=6, ipady=4)

        self.hud_sw_ac = ctk.CTkSwitch(h_ctrl, text="Inversor AC (1800W)", font=("Segoe UI", 10, "bold"), command=self._on_toggle_ac, progress_color="#ff3366")
        self.hud_sw_ac.pack(fill="x", padx=14, pady=4)

        self.hud_sw_dc = ctk.CTkSwitch(h_ctrl, text="Salidas DC (12V/USB)", font=("Segoe UI", 10, "bold"), command=self._on_toggle_dc, progress_color="#00f0ff")
        self.hud_sw_dc.pack(fill="x", padx=14, pady=4)

    def _create_hud_metric(self, parent, title, val, sub, color, r, c):
        f = ctk.CTkFrame(parent, fg_color="#080b11", corner_radius=10, border_width=1, border_color="#1e293b")
        f.grid(row=r, column=c, padx=3, pady=3, sticky="nsew", ipady=2)
        ctk.CTkLabel(f, text=title, font=("Segoe UI", 9, "bold"), text_color="#94a3b8").pack(anchor="w", padx=8, pady=(4, 0))
        lbl_v = ctk.CTkLabel(f, text=val, font=("Segoe UI", 16, "bold"), text_color=color)
        lbl_v.pack(anchor="w", padx=8)
        lbl_s = ctk.CTkLabel(f, text=sub, font=("Consolas", 9), text_color="#64748b")
        lbl_s.pack(anchor="w", padx=8, pady=(0, 4))
        return (lbl_v, lbl_s)

    # =========================================================================
    # CAMBIO ENTRE MODOS
    # =========================================================================
    def _switch_to_full_dashboard(self):
        self._show_full_dashboard()

    def _show_full_dashboard(self):
        self.is_gamer_hud_mode = False
        self.hud_container.pack_forget()
        self.main_container.pack(fill="both", expand=True)

        self.overrideredirect(False)
        self.wm_attributes("-topmost", False)
        self.attributes("-alpha", 1.0)
        self.geometry("1000x740")

    def _switch_to_gamer_hud(self):
        self.is_gamer_hud_mode = True
        self.main_container.pack_forget()
        self.hud_container.pack(fill="both", expand=True)

        screen_w = self.winfo_screenwidth()
        pos_x = max(0, screen_w - 380 - 15)
        self.geometry(f"360x520+{pos_x}+15")

        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)
        self.attributes("-alpha", 0.95)

    def _start_drag(self, event):
        self._drag_start_x = event.x
        self._drag_start_y = event.y

    def _do_drag(self, event):
        deltax = event.x - self._drag_start_x
        deltay = event.y - self._drag_start_y
        x = self.winfo_x() + deltax
        y = self.winfo_y() + deltay
        self.geometry(f"+{x}+{y}")

    # =========================================================================
    # CONTROLADORES DE EVENTOS Y TELEMETRÍA
    # =========================================================================
    def _on_toggle_ac(self):
        self.last_user_click_ac = time.time()
        active = bool(self.sw_ac.get())
        if self.is_gamer_hud_mode:
            active = bool(self.hud_sw_ac.get())
            if active != bool(self.sw_ac.get()):
                if active: self.sw_ac.select()
                else: self.sw_ac.deselect()
        else:
            if active: self.hud_sw_ac.select()
            else: self.hud_sw_ac.deselect()
        self.ble.set_ac_output(active)

    def _on_toggle_dc(self):
        self.last_user_click_dc = time.time()
        active = bool(self.sw_dc.get())
        if self.is_gamer_hud_mode:
            active = bool(self.hud_sw_dc.get())
            if active != bool(self.sw_dc.get()):
                if active: self.sw_dc.select()
                else: self.sw_dc.deselect()
        else:
            if active: self.hud_sw_dc.select()
            else: self.hud_sw_dc.deselect()
        self.ble.set_dc_output(active)

    def _on_toggle_eco(self):
        self.ble.set_eco_mode(bool(self.sw_eco.get()))

    def _on_toggle_grid(self):
        self.ble.set_grid_enhancement(bool(self.sw_grid.get()))

    def _on_toggle_pl(self):
        self.ble.set_power_lifting(bool(self.sw_pl.get()))

    def _set_charge_mode(self, mode: str):
        self.ble.set_charge_mode(mode)
        self.btn_charge_silent.configure(fg_color="#1d4ed8" if mode == "silent" else "#1e293b")
        self.btn_charge_standard.configure(fg_color="#1d4ed8" if mode == "standard" else "#1e293b")
        self.btn_charge_turbo.configure(fg_color="#1d4ed8" if mode == "turbo" else "#1e293b")

    def _toggle_simulation(self):
        self.ble.toggle_simulation_mode()
        is_sim = self.ble.simulation_mode
        self.btn_sim_mode.configure(
            text="🧪 Demo Activo" if is_sim else "🧪 Modo Demo / Simular",
            fg_color="#047857" if is_sim else "#065f46"
        )

    def _toggle_ble_release(self):
        if not self.ble.is_paused:
            self.ble.pause_and_release()
            self.btn_release_ble.configure(text="▶ Reanudar BLE", fg_color="#1d4ed8")
        else:
            self.ble.resume_connection()
            self.btn_release_ble.configure(text="⏸ Liberar BLE (Móvil)", fg_color="#1e293b")

    def _on_device_selected(self, choice: str):
        if " [" in choice and "]" in choice:
            addr = choice.split("[")[-1].replace("]", "").strip()
            self.ble.set_device_address(addr)

    def _handle_status(self, msg: str):
        self.after(0, lambda: self.lbl_status.configure(text=f"● {msg}"))

    def _handle_devices_discovered(self, devices: List[Dict[str, str]]):
        opts = [f"{d['name']} [{d['address']}]" for d in devices]
        if opts:
            self.after(0, lambda: self.combo_devices.configure(values=opts))

    def _handle_telemetry(self, t: Dict[str, Any]):
        self.after(0, lambda: self._update_ui_telemetry(t))

    def _update_ui_telemetry(self, t: Dict[str, Any]):
        soc = t.get("soc", 84)
        in_w = t.get("dc_input_watts", 0) + t.get("ac_input_watts", 0)
        out_w = t.get("ac_output_watts", 0) + t.get("dc_output_watts", 0)
        net = in_w - out_w

        # Actualizar colores según SoC
        soc_color = "#00ff88" if soc > 50 else ("#fbbf24" if soc > 20 else "#ff3366")

        self.lbl_soc_big.configure(text=f"{soc}%", text_color=soc_color)
        self.progress_soc_big.set(soc / 100.0)
        self.progress_soc_big.configure(progress_color=soc_color)
        self.lbl_wh_estimate.configure(text=f"{int(soc * 10.24)} / 1024 Wh Disponibles")
        self.lbl_batt_volts.configure(text=f"Tensión Celda: {t.get('battery_volts', 51.2):.1f} V")

        self.lbl_in_total_w.configure(text=f"{in_w} W")
        self.lbl_solar_val.configure(text=f"{t.get('dc_input_watts', 0)} W")
        v_sol = t.get("dc_input_volts", 0.0)
        i_sol = t.get("dc_input_current", 0.0)
        sol_text = f"{v_sol:.1f}V • {i_sol:.1f}A" if v_sol > 0 else "0.0V • 0.0A (Sin Entrada Solar)"
        self.lbl_solar_sub.configure(text=sol_text)

        v_grid = t.get("ac_input_volts", 0.0)
        i_grid = t.get("ac_input_current", 0.0)
        grid_text = f"{v_grid:.1f}V • {i_grid:.1f}A" if v_grid > 0 else "0.0V • 0.0A (Desconectada de Red)"
        self.lbl_ac_in_sub.configure(text=grid_text)

        self.lbl_out_total_w.configure(text=f"{out_w} W")
        self.lbl_ac_out_val.configure(text=f"{t.get('ac_output_watts', 0)} W")
        self.lbl_dc_out_val.configure(text=f"{t.get('dc_output_watts', 0)} W")

        net_text = f"Cargando (+{net}W)" if net > 5 else (f"Descargando ({net}W)" if net < -5 else "Flujo Neto: 0 W")
        self.lbl_net_power.configure(text=net_text, text_color="#00f0ff" if net > 5 else ("#ff3366" if net < -5 else "#94a3b8"))

        # Actualizar HUD Gamer
        self.hud_soc_val.configure(text=f"{soc}%", text_color=soc_color)
        self.hud_progress_soc.set(soc / 100.0)
        self.hud_progress_soc.configure(progress_color=soc_color)
        self.hud_net_power.configure(text=net_text)

        self.hud_solar_w[0].configure(text=f"{t.get('dc_input_watts', 0)} W")
        self.hud_grid_w[0].configure(text=f"{t.get('ac_input_watts', 0)} W")
        self.hud_ac_out_w[0].configure(text=f"{t.get('ac_output_watts', 0)} W")
        self.hud_dc_out_w[0].configure(text=f"{t.get('dc_output_watts', 0)} W")

        # Sincronizar estado de los conmutadores con los datos recibidos (con debouncing de clic)
        now = time.time()
        last_ac = getattr(self, "last_user_click_ac", 0)
        if "ac_output_on" in t and (now - last_ac > 2.0):
            ac_state = bool(t["ac_output_on"])
            if bool(self.sw_ac.get()) != ac_state:
                if ac_state: self.sw_ac.select()
                else: self.sw_ac.deselect()
            if bool(self.hud_sw_ac.get()) != ac_state:
                if ac_state: self.hud_sw_ac.select()
                else: self.hud_sw_ac.deselect()

        last_dc = getattr(self, "last_user_click_dc", 0)
        if "dc_output_on" in t and (now - last_dc > 2.0):
            dc_state = bool(t["dc_output_on"])
            if bool(self.sw_dc.get()) != dc_state:
                if dc_state: self.sw_dc.select()
                else: self.sw_dc.deselect()
            if bool(self.hud_sw_dc.get()) != dc_state:
                if dc_state: self.hud_sw_dc.select()
                else: self.hud_sw_dc.deselect()

        if "eco_mode_on" in t:
            eco_state = bool(t["eco_mode_on"])
            if bool(self.sw_eco.get()) != eco_state:
                if eco_state: self.sw_eco.select()
                else: self.sw_eco.deselect()

        if "grid_enhancement_on" in t:
            grid_state = bool(t["grid_enhancement_on"])
            if bool(self.sw_grid.get()) != grid_state:
                if grid_state: self.sw_grid.select()
                else: self.sw_grid.deselect()

        if "power_lifting_on" in t:
            pl_state = bool(t["power_lifting_on"])
            if bool(self.sw_pl.get()) != pl_state:
                if pl_state: self.sw_pl.select()
                else: self.sw_pl.deselect()

        if "charge_mode" in t:
            m = t["charge_mode"]
            self.btn_charge_silent.configure(fg_color="#1d4ed8" if m == "silent" else "#1e293b")
            self.btn_charge_standard.configure(fg_color="#1d4ed8" if m == "standard" else "#1e293b")
            self.btn_charge_turbo.configure(fg_color="#1d4ed8" if m == "turbo" else "#1e293b")


def main():
    app = BluettiDesktopApp()
    app.mainloop()


if __name__ == "__main__":
    main()
