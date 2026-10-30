"""HIS Packaging Line dashboard. Python 3.10+, Tkinter; optional python-opcua."""
import argparse
import json
import math
import os
from pathlib import Path
import queue
import time
import tkinter as tk
from tkinter import ttk, messagebox

from demo_model import DemoModel
from opc_link import OpcLink, SIGNALS

ROOT = Path(__file__).resolve().parent
BG = '#09111f'
PANEL = '#111e30'
EDGE = '#23354c'
TEXT = '#eaf1fc'
MUTED = '#8a9db7'
MINT = '#51e0b5'
BLUE = '#60a5fa'
AMBER = '#ffc86b'
RED = '#ff7182'
STATES = {0: 'WAITING FOR ENABLE', 10: 'LINE READY', 20: 'CARTON INFEED',
          30: 'SEALING CARTON', 40: 'CARTON OUTFEED',
          50: 'CARTON COMPLETE', 90: 'CARTON JAMMED'}


class Dashboard:
    def __init__(self, root, config_path):
        self.root = root
        self.config_path = config_path
        self.config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        self.events = queue.Queue()
        self.link = OpcLink(self.events)
        self.mode = 'demo'
        self.connected = False
        self.last_sample = 0
        self.password = os.getenv('HIS_OPCUA_PASSWORD', '')
        self.model = DemoModel()
        self.model.command('enable', True)
        self.model.command('sim_estop', True)
        self.model.command('reset')
        self.values = self.model.values.copy()
        self.last_time = time.monotonic()
        self.last_state = None
        self.phase_started = self.last_time
        self.belt = 0.0
        self.allow_write = False
        self.message = 'Local preview. Not connected to a PLC.'
        self.history = []
        self.actions = []
        self.settings = None
        root.title('HIS · Packaging Line')
        root.geometry('1360x860')
        root.minsize(1050, 700)
        root.configure(bg=BG)
        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Button-1>', self.click)
        self.canvas.bind('<Motion>', self.hover)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self.tick()

    def close(self):
        self.allow_write = False
        self.link.disconnect()
        # Let an in-flight pulse finish its FALSE write before Python exits.
        deadline = time.monotonic() + float(self.config.get('timeout_s', 3))*2 + 1
        def finish():
            if self.link.thread and self.link.thread.is_alive() and time.monotonic() < deadline:
                self.root.after(50, finish)
            else:
                self.root.destroy()
        self.root.withdraw()
        finish()

    def log(self, text):
        self.message = text
        self.history = [(time.strftime('%H:%M:%S'), text)] + self.history[:5]

    def connected_and_fresh(self):
        return self.connected and time.monotonic()-self.last_sample < max(1, self.config['poll_ms']/1000*4)

    def active(self):
        return self.mode == 'demo' or self.connected_and_fresh()

    def select_demo(self):
        if self.mode == 'demo':
            return
        self.link.disconnect()
        self.mode = 'demo'
        self.connected = False
        self.allow_write = False
        self.values = self.model.values.copy()
        self.last_state = None
        self.log('Local preview active. No PLC writes.')

    def toggle_connection(self):
        if self.connected or (self.link.thread and self.link.thread.is_alive()):
            self.link.disconnect()
            self.connected = False
            self.allow_write = False
            self.log('Disconnecting.')
        else:
            try:
                self.mode = 'opc'
                self.values = {}
                self.last_state = None
                self.last_sample = 0
                self.allow_write = False
                self.link.connect(self.config, self.password)
                self.log('Connecting …')
            except Exception as exc:
                self.log(str(exc))

    def toggle_writes(self):
        if self.mode == 'opc' and self.connected_and_fresh():
            if not self.allow_write and not all(k in self.values for k in ('state','permit')):
                self.log('Map Line.State and Safety.S_Permit before enabling PLC controls.')
                return
            self.allow_write = not self.allow_write
            self.log('PLC controls ' + ('enabled.' if self.allow_write else 'disabled.'))

    def command(self, key, toggle=False):
        if not self.active():
            return
        value = not bool(self.values.get(key)) if toggle else True
        if self.mode == 'demo':
            self.model.command(key, value)
            self.values = self.model.values.copy()
            self.log({'start': 'Packaging cycle started.', 'stop': 'Process stop.',
                      'reset': 'Safety reset requested.', 'ack': 'Jam acknowledgement requested.'}.get(
                          key, SIGNALS[key] + ' = ' + str(value)))
        elif self.allow_write:
            try:
                self.link.write(key, value, pulse=key in ('start', 'stop', 'reset', 'ack'))
            except Exception as exc:
                self.log(str(exc))

    def can_command(self, key):
        if not self.active():
            return False
        if self.mode == 'demo':
            return True
        return (self.allow_write and bool(self.config['nodes'].get(key))
                and (key != 'sim_estop' or self.config.get('simulation_input_enabled', False))
                and (key not in ('enable', 'fast', 'jam', 'sim_estop') or type(self.values.get(key)) is bool))

    def tick(self):
        now = time.monotonic()
        dt = min(now-self.last_time, 0.1)
        self.last_time = now
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if self.mode != 'opc':
                continue
            if kind == 'connected':
                self.connected = True
                self.log('OPC UA connected · read-only mode.')
            elif kind == 'snapshot':
                self.values = payload
                self.last_sample = now
            elif kind in ('message', 'error'):
                self.log(payload)
            elif kind == 'disconnected':
                self.connected = False
                self.allow_write = False
            elif kind == 'written':
                self.log('Written to PLC: ' + SIGNALS[payload])
            elif kind == 'browse' and self.settings and self.settings.winfo_exists():
                self.settings.show_browse(payload)
        if self.mode == 'demo':
            self.values = self.model.step(dt)
        state = self.values.get('state')
        if state != self.last_state:
            self.phase_started = now
            self.last_state = state
        if self.active() and state in (20, 40):
            self.belt = (self.belt+dt*80) % 36
        self.draw(now)
        self.root.after(33, self.tick)

    def draw(self, now):
        c = self.canvas
        c.delete('all')
        self.actions = []
        self.sx = max(c.winfo_width(), 1)/1360
        self.sy = max(c.winfo_height(), 1)/860
        self.sf = min(self.sx, self.sy)
        v = self.values
        fresh = self.active()
        state = v.get('state')
        title = STATES.get(state, 'WAITING FOR PLC DATA') if fresh else 'CONNECTION LOST'
        if self.mode == 'opc' and self.connected and 'state' not in v:
            title = 'CONFIGURE NODE MAPPING'
        status_color = RED if not fresh or state == 90 else (MINT if state in (10,50) else BLUE)

        self.text(32, 29, 'NEURON  /  HIS', 12, MINT, bold=True)
        self.text(32, 61, 'Packaging Line', 30, TEXT, bold=True)
        self.text(32, 95, 'SPS 2026  ·  Virtual packaging line', 12, MUTED)
        self.button(785, 28, 150, 36, 'Local demo', self.select_demo,
                    fill='#1b3b3b' if self.mode=='demo' else PANEL)
        self.button(947,28,170,36, 'Disconnect' if self.connected else 'Connect OPC UA', self.toggle_connection)
        self.button(1129,28,198,36,'Connection & Node IDs',self.open_settings)
        badge = 'DEMO · LOCAL' if self.mode=='demo' else ('OPC UA · LIVE' if self.connected_and_fresh() else 'OPC UA · OFFLINE')
        self.text(1327,92,badge,11,MINT if fresh else RED,anchor='e',bold=True)

        self.box(28,126,974,405,PANEL)
        self.text(52,150,'PRODUCTION CELL',10,MUTED,bold=True)
        self.text(52,177,title,21,status_color,bold=True)
        self.text(974,153,'01  /  CARTON PACKAGING',10,MUTED,anchor='e')
        # Production line labels and background guide.
        for x, number, name in [(87,'01','INFEED'),(440,'02','SEALING'),(765,'03','OUTFEED')]:
            self.text(x,226,number,11,BLUE,bold=True)
            self.text(x+30,226,name,10,MUTED,bold=True)
        self.line(64,364,960,364,EDGE,2)
        # Conveyor deck, lower frame, feet and moving slats.
        self.box(64,364,896,44,'#26394f',radius=16)
        self.box(78,373,868,24,'#172739',radius=8)
        for x in range(90,950,36):
            xx=x+self.belt
            if xx<943:
                self.line(xx,376,xx-9,394,'#3d5269',2)
        for x in (124,310,728,902):
            self.rect(x,408,x+13,442,'#23384f')
            self.rect(x-9,439,x+23,445,'#3f566f')
        for x in (86,935):
            self.oval(x-12,372,x+12,396,'#61738a')
            self.oval(x-5,379,x+5,389,'#192b40')
        # Sealing station portal.
        self.box(452,256,155,33,'#2b435d',radius=7)
        self.rect(465,286,478,365,'#476582')
        self.rect(581,286,594,365,'#476582')
        self.box(486,283,87,26,'#344f6b',radius=5)
        head_y=315+((1-math.cos((now-self.phase_started)*5))*9 if state==30 and fresh else 0)
        self.rect(522,309,537,head_y+8,'#7b91a7')
        self.box(493,head_y,72,12,MINT if state==30 and fresh else '#7293ae',radius=3)
        self.text(529,271,'SEAL / 02',10,TEXT,anchor='center',bold=True)
        # Accumulation cartons and active carton position.
        self.carton(99,329,33,False,muted=True)
        self.carton(144,329,33,False,muted=True)
        duration = (1.5 if v.get('fast_active') else 3) if state==20 else (1 if v.get('fast_active') else 2)
        # Position is interpolated only for presentation; it is not a sensor value.
        elapsed=now-self.phase_started
        if not fresh:
            elapsed=0
        progress=min(1,max(0,elapsed/duration))
        x=529
        visible=state in (20,30,40,90)
        if state==20:
            x=227+302*progress
        elif state==40:
            x=529+350*progress
        if visible:
            self.carton(x-29,313,58,state==40, jam=state==90)
        if state==50 and fresh:
            self.carton(859,313,58,True)
            self.text(892,296,'✓',21,MINT,anchor='center',bold=True)
        # Station sensors / purely illustrative indicators.
        for x, lit in [(420,state in (30,90)),(793,state==40)]:
            self.rect(x,352,x+8,366,'#596f88')
            self.oval(x-1,348,x+9,358,MINT if lit and fresh else '#31475f')
        self.text(63,483,'Animation based on phase and time · carton position is illustrative',10,MUTED)

        self.box(1020,126,312,125,PANEL)
        self.text(1044,150,'CARTONS PACKED',10,MUTED,bold=True)
        count=v.get('count')
        self.text(1044,205,f'{count:04d}' if type(count) is int else '—',43,TEXT,bold=True)
        self.text(1307,220,'CARTONS',10,MUTED,anchor='e')
        self.box(1020,267,312,126,PANEL)
        self.text(1044,291,'SAFETY PERMISSION',10,MUTED,bold=True)
        permit=v.get('permit') if fresh else None
        self.oval(1045,323,1059,337,MINT if permit is True else RED if permit is False else MUTED)
        self.text(1072,328,'ENABLED' if permit is True else 'DISABLED' if permit is False else 'UNKNOWN',16,
                  MINT if permit else MUTED,bold=True)
        self.text(1044,366,'PLC status · GUI does not provide a safety function',9,MUTED)
        self.box(1020,409,312,122,PANEL)
        self.text(1044,434,'OPERATING MODE',10,MUTED,bold=True)
        self.text(1044,466,'FAST CYCLE' if v.get('fast_active') else 'NORMAL CYCLE',17,BLUE,bold=True)
        self.text(1044,502,'State: '+str(state) if state is not None else 'State: —',11,MUTED)

        # Four phase tiles.
        for i,(phase,label,hint) in enumerate([(20,'Infeed','Move carton to station'),(30,'Sealing','Seal the carton'),
                                             (40,'Outfeed','Move carton out'),(50,'Complete','Counter +1')]):
            x=28+i*247
            selected=state==phase and fresh
            self.box(x,548,233,76,'#183b40' if selected else PANEL)
            self.text(x+16,567,f'0{i+1}   '+label,13,MINT if selected else TEXT,bold=True)
            self.text(x+16,597,hint,10,MUTED)
        self.box(1020,548,312,76,PANEL)
        eq=v.get('equivalent_diag'); es=v.get('estop_diag')
        self.text(1044,568,'SAFETY DIAGNOSTICS',10,MUTED,bold=True)
        self.text(1044,597,'EQ '+(f'{eq:04X}' if type(eq)is int else '—')+'   /   E-STOP '+(f'{es:04X}' if type(es)is int else '—'),12,TEXT)

        self.text(30,653,'OPERATOR PANEL',10,MUTED,bold=True)
        if self.mode=='opc':
            self.button(1020,641,312,30,'PLC controls: '+('ENABLED' if self.allow_write else 'READ ONLY'),
                        self.toggle_writes,enabled=self.connected_and_fresh(),fill='#493924' if self.allow_write else PANEL)
        def b(x,w,label,key,color=PANEL,toggle=False):
            self.button(x,680,w,43,label,lambda k=key,t=toggle:self.command(k,t),
                        fill=color,enabled=self.can_command(key))
        b(28,130,'▶  Start','start','#155b50')
        b(170,120,'■  Stop','stop')
        b(302,150,'Safety reset','reset')
        b(464,160,'Acknowledge jam','ack')
        b(636,170,'Operation '+('ON' if v.get('enable') else 'OFF'),'enable',toggle=True)
        b(818,190,'Fast cycle '+('ON' if v.get('fast') else 'OFF'),'fast',toggle=True)
        b(1020,312,'Carton jam '+('ON' if v.get('jam') else 'OFF'),'jam',
          '#593d29' if v.get('jam') else PANEL,toggle=True)
        if self.mode=='demo' or self.config.get('simulation_input_enabled'):
            released=v.get('sim_estop')
            self.button(28,739,280,40,'Sim. E-stop: '+('RELEASED' if released else 'PRESSED'),
                        lambda:self.command('sim_estop',True),enabled=self.can_command('sim_estop'),fill='#592c3b')
            self.text(325,759,'For the RTS Max simulation program only',10,MUTED)
        else:
            self.text(30,758,'Operate emergency stop on the physical demo box.',11,MUTED)
        age=now-self.last_sample
        footer='Local model · no PLC connection' if self.mode=='demo' else (
            f'OPC UA · polling {self.config["poll_ms"]} ms · last read {age:.1f} s ago' if self.last_sample else 'OPC UA · no valid data received yet')
        self.line(28,798,1332,798,EDGE)
        self.text(30,815,self.message[:145],11,MUTED)
        self.text(1330,844,footer,9,MUTED,anchor='e')

    def carton(self,x,y,size,sealed,jam=False,muted=False):
        color='#655540' if muted else '#d5a765'
        self.box(x,y,size,size*.83,color,radius=4)
        self.line(x,y+size*.2,x+size,y+size*.2,'#8e6a3c',1)
        self.rect(x+size*.43,y,x+size*.57,y+size*.83,'#ad814c')
        if sealed:
            self.rect(x+size*.43,y,x+size*.57,y+size*.83,'#61dbc1')
        if not muted:
            self.text(x+size*.22,y+size*.53,'HIS',9,'#523d25',bold=True)
        if jam:
            self.text(x+size/2,y-15,'!',20,RED,anchor='center',bold=True)

    def text(self,x,y,text,size=12,color=TEXT,anchor='w',bold=False):
        self.canvas.create_text(x*self.sx,y*self.sy,text=text,fill=color,anchor=anchor,
                                font=('Segoe UI',max(8,int(size*self.sf)),'bold' if bold else 'normal'))

    def rect(self,x1,y1,x2,y2,color):
        self.canvas.create_rectangle(x1*self.sx,y1*self.sy,x2*self.sx,y2*self.sy,fill=color,outline='')

    def oval(self,x1,y1,x2,y2,color):
        self.canvas.create_oval(x1*self.sx,y1*self.sy,x2*self.sx,y2*self.sy,fill=color,outline='')

    def line(self,x1,y1,x2,y2,color,width=1):
        self.canvas.create_line(x1*self.sx,y1*self.sy,x2*self.sx,y2*self.sy,fill=color,width=max(1,width*self.sf))

    def box(self,x,y,w,h,fill,radius=12):
        r=min(radius,w/2,h/2)
        points=[x+r,y,x+w-r,y,x+w,y,x+w,y+r,x+w,y+h-r,x+w,y+h,x+w-r,y+h,
                x+r,y+h,x,y+h,x,y+h-r,x,y+r,x,y]
        coords=[v*(self.sx if i%2==0 else self.sy) for i,v in enumerate(points)]
        self.canvas.create_polygon(coords,smooth=True,splinesteps=20,fill=fill,outline='')

    def button(self,x,y,w,h,label,action,fill=PANEL,enabled=True):
        self.box(x,y,w,h,fill if enabled else '#131d2b',8)
        self.text(x+w/2,y+h/2,label,11,TEXT if enabled else '#4e6077',anchor='center',bold=True)
        if enabled:
            self.actions.append((x,y,x+w,y+h,action))

    def click(self,event):
        x,y=event.x/self.sx,event.y/self.sy
        for x1,y1,x2,y2,action in self.actions:
            if x1<=x<=x2 and y1<=y<=y2:
                action()
                break

    def hover(self,event):
        x,y=event.x/self.sx,event.y/self.sy
        self.canvas.configure(cursor='hand2' if any(a<=x<=c and b<=y<=d for a,b,c,d,_ in self.actions) else '')

    def open_settings(self):
        if self.settings and self.settings.winfo_exists():
            self.settings.lift()
            return
        self.settings = Settings(self)


class Settings(tk.Toplevel):
    def __init__(self,app):
        super().__init__(app.root)
        self.app=app
        self.title('OPC UA · Connection and variable mapping')
        self.geometry('1020x760')
        self.configure(bg=BG)
        style=ttk.Style(self)
        style.theme_use('clam')
        style.configure('TFrame',background=BG)
        style.configure('TLabel',background=BG,foreground=TEXT)
        style.configure('Treeview',rowheight=25)
        notebook=ttk.Notebook(self)
        notebook.pack(fill='both',expand=True,padx=14,pady=14)
        connection=ttk.Frame(notebook,padding=18)
        mapping=ttk.Frame(notebook,padding=12)
        browsing=ttk.Frame(notebook,padding=12)
        notebook.add(connection,text='Connection')
        notebook.add(mapping,text='Node-IDs')
        notebook.add(browsing,text='Browse server')
        self.fields={}
        labels=[('endpoint','Endpoint'),('username','Username (blank = anonymous)'),
                ('password','Password (not saved)'),('security','Security string (blank = None)'),
                ('poll_ms','Polling interval in ms'),('timeout_s','Timeout in seconds')]
        for row,(key,label) in enumerate(labels):
            ttk.Label(connection,text=label).grid(row=row,column=0,sticky='w',pady=10)
            var=tk.StringVar(value=app.password if key=='password' else str(app.config.get(key,'')))
            self.fields[key]=var
            ttk.Entry(connection,textvariable=var,width=70,show='*' if key=='password' else '').grid(row=row,column=1,padx=15,sticky='ew')
        self.sim=tk.BooleanVar(value=app.config.get('simulation_input_enabled',False))
        ttk.Checkbutton(connection,text='RTS Max simulation program: enable Sim_EStopReleased control',variable=self.sim).grid(row=6,column=0,columnspan=2,sticky='w',pady=15)
        ttk.Label(connection,text='Leave disabled for the physical demo box. The GUI does not write safe outputs.').grid(row=7,column=0,columnspan=2,sticky='w')
        ttk.Label(connection,text='Security example: Basic256Sha256,SignAndEncrypt,client.der,client-key.pem,server.der',wraplength=850).grid(row=8,column=0,columnspan=2,sticky='w',pady=15)
        ttk.Label(connection,text='Use absolute certificate paths. Reconnect to apply changes.\nA connection failure does not automatically change the security method.',wraplength=850).grid(row=9,column=0,columnspan=2,sticky='w')
        connection.columnconfigure(1,weight=1)
        ttk.Label(mapping,text='Enter exact Node IDs or assign them in the server browser. Node IDs are not guessed.').pack(anchor='w',pady=(0,10))
        frame=ttk.Frame(mapping)
        frame.pack(fill='both',expand=True)
        cv=tk.Canvas(frame,bg=BG,highlightthickness=0)
        scroll=ttk.Scrollbar(frame,orient='vertical',command=cv.yview)
        cv.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y'); cv.pack(side='left',fill='both',expand=True)
        inner=ttk.Frame(cv)
        cv.create_window((0,0),window=inner,anchor='nw')
        inner.bind('<Configure>',lambda e:cv.configure(scrollregion=cv.bbox('all')))
        self.nodes={}
        for row,(key,name) in enumerate(SIGNALS.items()):
            ttk.Label(inner,text=name,width=27).grid(row=row,column=0,sticky='w',pady=4)
            var=tk.StringVar(value=app.config['nodes'].get(key,''))
            self.nodes[key]=var
            ttk.Entry(inner,textvariable=var,width=86).grid(row=row,column=1,pady=4)
        bar=ttk.Frame(browsing); bar.pack(fill='x')
        ttk.Button(bar,text='Load Objects',command=self.browse_root).pack(side='left')
        self.selected_signal=tk.StringVar(value='state')
        ttk.Combobox(bar,textvariable=self.selected_signal,values=list(SIGNALS),state='readonly',width=20).pack(side='left',padx=10)
        ttk.Button(bar,text='Assign selected node',command=self.assign).pack(side='left')
        self.tree=ttk.Treeview(browsing,columns=('nodeid','class'),show='tree headings')
        self.tree.heading('#0',text='Name'); self.tree.column('#0',width=280)
        self.tree.heading('nodeid',text='Node-ID'); self.tree.column('nodeid',width=510)
        self.tree.heading('class',text='Type'); self.tree.column('class',width=90)
        self.tree.pack(fill='both',expand=True,pady=10)
        self.tree.bind('<<TreeviewOpen>>',self.expand)
        self.browse_note=tk.StringVar(value='Connect to the server in the main window first. Browsing is read-only.')
        ttk.Label(browsing,textvariable=self.browse_note).pack(anchor='w')
        ttk.Button(self,text='Save configuration',command=self.save).pack(pady=(0,14))

    def browse_root(self):
        try:
            for item in self.tree.get_children():self.tree.delete(item)
            self.app.link.browse('i=85','root')
        except Exception as exc:messagebox.showerror('OPC UA',str(exc),parent=self)

    def expand(self,event):
        item=self.tree.focus()
        if not item:return
        children=self.tree.get_children(item)
        if children and self.tree.item(children[0],'text')=='…':
            try:self.app.link.browse(self.tree.item(item,'values')[0],item)
            except Exception as exc:messagebox.showerror('OPC UA',str(exc),parent=self)

    def show_browse(self,payload):
        token,rows,truncated=payload
        parent='' if token=='root' else token
        if parent and not self.tree.exists(parent):return
        for child in self.tree.get_children(parent):self.tree.delete(child)
        for name,nodeid,kind in rows:
            item=self.tree.insert(parent,'end',text=name,values=(nodeid,kind))
            if kind in ('Object','Variable'):
                self.tree.insert(item,'end',text='…')
        self.browse_note.set(f'{len(rows)} nodes loaded.'+(' Display limited to 250 child nodes.' if truncated else ''))

    def assign(self):
        selection=self.tree.selection()
        if not selection:return
        values=self.tree.item(selection[0],'values')
        if len(values)<2 or values[1]!='Variable':
            messagebox.showinfo('Node-ID','Please select a Variable node.',parent=self);return
        key=self.selected_signal.get()
        self.nodes[key].set(values[0])
        self.browse_note.set(SIGNALS[key]+' ← '+values[0]+' (not saved yet)')

    def save(self):
        try:
            endpoint=self.fields['endpoint'].get().strip()
            if not endpoint.startswith('opc.tcp://'):
                raise ValueError('Endpoint must start with opc.tcp://.')
            poll=int(self.fields['poll_ms'].get()); timeout=float(self.fields['timeout_s'].get())
            if not 50<=poll<=5000 or not 0.2<=timeout<=30:
                raise ValueError('Polling: 50–5000 ms; timeout: 0.2–30 s.')
            config=dict(endpoint=endpoint,username=self.fields['username'].get().strip(),
                        security=self.fields['security'].get().strip(),poll_ms=poll,timeout_s=timeout,
                        simulation_input_enabled=self.sim.get(),nodes={k:v.get().strip() for k,v in self.nodes.items()})
            # Prevent one writable node from accidentally controlling multiple commands.
            from opc_link import WRITABLE
            writable=[config['nodes'][k] for k in WRITABLE if config['nodes'].get(k)]
            if len(writable)!=len(set(writable)):
                raise ValueError('Control variables require distinct Node IDs.')
            self.app.config_path.write_text(json.dumps(config,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
            # New labels/mappings must never address an old connected session.
            if self.app.link.thread and self.app.link.thread.is_alive():
                self.app.link.disconnect()
                self.app.connected=False
            self.app.config=config
            self.app.password=self.fields['password'].get()
            self.app.allow_write=False
            self.app.log('Configuration saved. Reconnect to apply the new mapping.')
            self.destroy()
        except Exception as exc:messagebox.showerror('Configuration',str(exc),parent=self)


def main():
    parser=argparse.ArgumentParser(description='HIS Packaging Line dashboard')
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--export-preview',type=Path,help='Render the local demo canvas to SVG and exit')
    args=parser.parse_args()
    root=tk.Tk()
    if args.export_preview:
        root.withdraw()
        app=Dashboard(root,args.config)
        app.canvas.winfo_width=lambda:1360
        app.canvas.winfo_height=lambda:860
        app.model.command('start')
        app.values=app.model.step(3.01)
        app.phase_started=time.monotonic()
        app.draw(time.monotonic())
        export_svg(app.canvas,args.export_preview)
        root.destroy()
    else:
        Dashboard(root,args.config)
        root.mainloop()


def export_svg(canvas,path):
    """Export actual canvas primitives, not an independently drawn mockup."""
    from html import escape
    body=['<svg xmlns="http://www.w3.org/2000/svg" width="1360" height="860" viewBox="0 0 1360 860">',
          '<rect width="1360" height="860" fill="'+BG+'"/>']
    for item in canvas.find_all():
        kind=canvas.type(item); p=canvas.coords(item)
        fill=canvas.itemcget(item,'fill') or 'none'
        if kind=='polygon':
            # Tk smooth polygons use a quadratic spline through edge midpoints.
            pts=list(zip(p[::2],p[1::2])); n=len(pts)
            start=((pts[-1][0]+pts[0][0])/2,(pts[-1][1]+pts[0][1])/2)
            commands=[f'M {start[0]} {start[1]}']
            for i,(x,y) in enumerate(pts):
                nx,ny=pts[(i+1)%n]
                commands.append(f'Q {x} {y} {(x+nx)/2} {(y+ny)/2}')
            body.append(f'<path d="{" ".join(commands)} Z" fill="{fill}"/>')
        elif kind=='rectangle':
            x,y,x2,y2=p
            body.append(f'<rect x="{x}" y="{y}" width="{x2-x}" height="{y2-y}" fill="{fill}"/>')
        elif kind=='oval':
            x,y,x2,y2=p
            body.append(f'<ellipse cx="{(x+x2)/2}" cy="{(y+y2)/2}" rx="{(x2-x)/2}" ry="{(y2-y)/2}" fill="{fill}"/>')
        elif kind=='line':
            body.append(f'<polyline points="{" ".join(map(str,p))}" fill="none" stroke="{fill}" stroke-width="{canvas.itemcget(item,"width")}"/>')
        elif kind=='text':
            x,y=p
            font=canvas.itemcget(item,'font')
            size=re_font_size(font)
            anchor={'w':'start','e':'end','center':'middle'}.get(canvas.itemcget(item,'anchor'),'start')
            weight='bold' if 'bold' in font else 'normal'
            body.append(f'<text x="{x}" y="{y}" fill="{fill}" font-family="Segoe UI, sans-serif" font-size="{size*1.3333}" font-weight="{weight}" text-anchor="{anchor}" dominant-baseline="middle">{escape(canvas.itemcget(item,"text"))}</text>')
    body.append('</svg>')
    path.write_text('\n'.join(body),encoding='utf-8')


def re_font_size(font):
    import re
    match=re.search(r'\}\s+(\d+)|\s(\d+)\s',font+' ')
    return int(next(g for g in match.groups() if g)) if match else 12


if __name__=='__main__':
    main()
