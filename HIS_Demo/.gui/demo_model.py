"""Local preview model. This is not the PLC runtime or a safety implementation."""
class DemoModel:
    def __init__(self):
        self.values = dict(state=0, count=0, permit=False, reset_request=False,
                           safety_error=False, equivalent_diag=0x8000, estop_diag=0x8802,
                           fault=False, fast_active=False, ready=False, enable=False,
                           fast=False, jam=False, sim_estop=False,
                           start=False, stop=False, reset=False, ack=False)
        self.elapsed = 0.0

    def command(self, key, value=True):
        v = self.values
        if key in ('enable', 'fast', 'jam', 'sim_estop'):
            v[key] = value
        elif key == 'reset' and v['sim_estop']:
            v['permit'] = True
        elif key == 'ack' and not v['jam'] and v['permit'] and v['enable']:
            v['fault'] = False
        elif key == 'stop':
            v['state'] = 10 if v['permit'] and v['enable'] else 0
            self.elapsed = 0
        elif key == 'start' and v['ready']:
            v['fast_active'] = v['fast']
            v['state'] = 20
            self.elapsed = 0
        self.step(0)

    def step(self, dt):
        v = self.values
        if not v['sim_estop']:
            v['permit'] = False
        v['reset_request'] = v['sim_estop'] and not v['permit']
        v['estop_diag'] = 0x8000 if v['permit'] else (0x8404 if v['reset_request'] else 0x8802)
        if v['jam'] and v['state'] in (20, 30, 40) and v['permit'] and v['enable']:
            v['fault'] = True
        if not v['enable'] or not v['permit']:
            v['state'] = 0
            v['fast_active'] = False
            self.elapsed = 0
        elif v['fault']:
            v['state'] = 90
            v['fast_active'] = False
            self.elapsed = 0
        elif v['state'] in (0, 90):
            v['state'] = 10
        elif v['state'] in (20, 30, 40, 50):
            self.elapsed += dt
            durations = {20: 1.5 if v['fast_active'] else 3,
                         30: 1 if v['fast_active'] else 2,
                         40: 1 if v['fast_active'] else 2, 50: 1}
            if self.elapsed >= durations[v['state']]:
                old = v['state']
                v['state'] = {20: 30, 30: 40, 40: 50, 50: 10}[old]
                if old == 40:
                    v['count'] = min(4294967295, v['count'] + 1)
                if old == 50:
                    v['fast_active'] = False
                self.elapsed = 0
        v['ready'] = v['state'] == 10 and not v['jam'] and not v['fault']
        return v.copy()
