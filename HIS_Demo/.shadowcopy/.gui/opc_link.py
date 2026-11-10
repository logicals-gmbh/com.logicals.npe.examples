"""OPC UA transport. All network calls run outside Tk's main thread."""
import queue
import threading
import time

SIGNALS = {
    'state': 'Line.State', 'count': 'Line.CartonsPacked',
    'permit': 'Safety.S_Permit', 'reset_request': 'Safety.ResetRequest',
    'safety_error': 'Safety.Error', 'equivalent_diag': 'Safety.EquivalentDiag',
    'estop_diag': 'Safety.EStopDiag', 'fault': 'Line.Fault',
    'fast_active': 'Line.FastCycleActive', 'ready': 'Line.Ready',
    'enable': 'CmdEnable', 'start': 'CmdStart', 'stop': 'CmdStop',
    'fast': 'CmdFastCycle', 'jam': 'CmdJam', 'reset': 'CmdSafetyReset',
    'ack': 'CmdAckJam', 'sim_estop': 'Sim_EStopReleased',
}
WRITABLE = {'enable', 'start', 'stop', 'fast', 'jam', 'reset', 'ack', 'sim_estop'}
PULSES = {'start', 'stop', 'reset', 'ack'}


class OpcLink:
    def __init__(self, events):
        self.events = events
        self.commands = queue.Queue()
        self.stop_event = threading.Event()
        self.thread = None
        self.connected = False

    def connect(self, config, password=''):
        if self.thread and self.thread.is_alive():
            raise RuntimeError('Connection is still closing. Please wait.')
        self.stop_event.clear()
        self.commands = queue.Queue()  # Never replay writes after reconnect.
        self.thread = threading.Thread(target=self._run, args=(config.copy(), password), daemon=True)
        self.thread.start()

    def disconnect(self):
        self.connected = False
        self.stop_event.set()

    def write(self, key, value, pulse=False):
        if key not in WRITABLE or type(value) is not bool:
            raise ValueError('Only approved Boolean control variables can be written.')
        if pulse and key not in PULSES:
            raise ValueError('Invalid pulse command.')
        if not self.connected:
            raise RuntimeError('No active OPC UA connection.')
        self.commands.put(('write', key, value, pulse))

    def browse(self, nodeid='i=85', token='root'):
        if not self.connected:
            raise RuntimeError('Connect to the OPC UA server first.')
        self.commands.put(('browse', nodeid, token))

    def _run(self, config, password):
        client = None
        try:
            from opcua import Client, ua
            client = Client(config['endpoint'], timeout=float(config.get('timeout_s', 3)))
            if config.get('security'):
                client.set_security_string(config['security'])
            if config.get('username'):
                client.set_user(config['username'])
                client.set_password(password)
            client.connect()
            namespaces = client.get_namespace_array()
            namespace_uri = config.get('namespace_uri')
            # Older HMI profiles use ns=1. Recognize the standalone gateway
            # by its stable application namespace URI, not an index.
            gateway_uri = 'urn:neuron:his:ncf-gateway'
            if not namespace_uri and gateway_uri in namespaces:
                namespace_uri = gateway_uri
            if namespace_uri and namespace_uri not in namespaces:
                raise ValueError('Configured OPC UA namespace URI is not present on this server: '+namespace_uri)
            index = namespaces.index(namespace_uri) if namespace_uri else None
            nodes = {}
            for key, value in config['nodes'].items():
                if not value or key not in SIGNALS:continue
                nodeid = ua.NodeId.from_string(value)
                if index is not None and nodeid.NamespaceIndex != 0:
                    nodeid = ua.NodeId(nodeid.Identifier, index, nodeid.NodeIdType)
                nodes[key] = client.get_node(nodeid)
            keys = list(nodes)
            self.connected = True
            self.events.put(('connected', config['endpoint']))
            deadline = 0
            while not self.stop_event.is_set():
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    command = None
                if command and command[0] == 'browse':
                    _, nodeid, token = command
                    try:
                        children = client.get_node(nodeid).get_children()
                        rows = []
                        for node in children[:250]:
                            rows.append((node.get_display_name().Text, node.nodeid.to_string(),
                                         node.get_node_class().name))
                        self.events.put(('browse', (token, rows, len(children) > 250)))
                    except Exception as exc:
                        self.events.put(('message', 'Browse failed: ' + str(exc)))
                elif command:
                    _, key, value, pulse = command
                    if key not in nodes:
                        self.events.put(('message', f'No Node ID configured for {SIGNALS[key]}.'))
                        continue
                    if key == 'sim_estop' and not config.get('simulation_input_enabled', False):
                        self.events.put(('message', 'Simulated emergency stop is disabled for this connection.'))
                        continue
                    node = nodes[key]
                    if node.get_data_type_as_variant_type() != ua.VariantType.Boolean:
                        raise ValueError(f'{SIGNALS[key]} is not a Boolean on the server. Write blocked.')
                    if pulse:
                        # Do not generate a new edge while the signal is already high.
                        if node.get_value() is not False:
                            self.events.put(('message', f'{SIGNALS[key]} is not FALSE. Reset the PLC signal first.'))
                            continue
                        try:
                            node.set_value(ua.Variant(True, ua.VariantType.Boolean))
                            self.stop_event.wait(0.25)
                        finally:
                            try:
                                node.set_value(ua.Variant(False, ua.VariantType.Boolean))
                            except Exception as exc:
                                raise RuntimeError(f'{SIGNALS[key]}: pulse reset not confirmed; check the PLC signal.') from exc
                    else:
                        node.set_value(ua.Variant(value, ua.VariantType.Boolean))
                    self.events.put(('written', key))
                if time.monotonic() >= deadline:
                    snapshot = {}
                    if keys:
                        results = client.uaclient.get_attributes([nodes[k].nodeid for k in keys], ua.AttributeIds.Value)
                        if len(results) != len(keys):
                            raise RuntimeError('Incomplete OPC UA response.')
                        for key, result in zip(keys, results):
                            if not result.StatusCode.is_good():
                                raise RuntimeError(f'{SIGNALS[key]}: {result.StatusCode}')
                            snapshot[key] = result.Value.Value
                        if 'state' in snapshot and (type(snapshot['state']) is not int or snapshot['state'] not in (0, 10, 20, 30, 40, 50, 90)):
                            raise ValueError('Line.State has an unexpected value or type. Check the mapping.')
                    self.events.put(('snapshot', snapshot))
                    deadline = time.monotonic() + max(0.05, float(config.get('poll_ms', 100)) / 1000)
                self.stop_event.wait(0.015)
        except Exception as exc:
            self.events.put(('error', str(exc)))
        finally:
            self.connected = False
            if client:
                try:
                    client.disconnect()
                except Exception:
                    pass
            self.events.put(('disconnected', None))
