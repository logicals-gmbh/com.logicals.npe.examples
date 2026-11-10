"""Run: python -m unittest -v test_gui. Uses a local OPC UA test server only."""
import copy
import json
from pathlib import Path
import queue
import socket
import time
import unittest
from demo_model import DemoModel
from opc_link import OpcLink


class ModelTests(unittest.TestCase):
    def ready_model(self):
        m=DemoModel()
        m.command('enable',True); m.command('sim_estop',True); m.command('reset')
        self.assertEqual(m.values['state'],10)
        return m

    def test_normal_and_fast_cycles_count_once(self):
        for fast,duration in [(False,8.0),(True,4.5)]:
            m=self.ready_model(); m.command('fast',fast); m.command('start')
            for _ in range(round((duration+0.3)/0.01)):m.step(0.01)
            self.assertEqual(m.values['count'],1)
            self.assertEqual(m.values['state'],10)
            for _ in range(1000):m.step(0.01)
            self.assertEqual(m.values['count'],1)

    def test_estop_and_jam_require_explicit_recovery(self):
        m=self.ready_model(); m.command('start'); m.step(0.5)
        m.command('jam',True)
        self.assertTrue(m.values['fault'])
        m.command('sim_estop',False); m.command('sim_estop',True)
        self.assertFalse(m.values['permit'])
        m.command('reset'); m.command('ack')
        self.assertTrue(m.values['fault'])
        m.command('jam',False); m.command('ack')
        self.assertEqual(m.values['state'],10)
        self.assertEqual(m.values['count'],0)
        m.command('start')
        self.assertEqual(m.values['state'],20)

    def test_gui_can_render_without_network(self):
        import tkinter as tk
        from his_gui import Dashboard
        root=tk.Tk(); root.withdraw()
        try:
            app=Dashboard(root,Path(__file__).with_name('config.json'))
            root.update_idletasks()
            app.canvas.configure(width=1360,height=860)
            for state in (0,10,20,30,40,50,90,None):
                app.values['state']=state
                app.draw(time.monotonic())
                self.assertGreater(len(app.canvas.find_all()),100)
        finally:root.destroy()


class OpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from opcua import Server,ua
        cls.ua=ua
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
        cls.endpoint=f'opc.tcp://127.0.0.1:{port}/his-test/'
        cls.server=Server(); cls.server.set_endpoint(cls.endpoint)
        ns=cls.server.register_namespace('urn:his:gui:test')
        obj=cls.server.get_objects_node().add_object(ns,'Packaging')
        cls.state=obj.add_variable(ns,'State',ua.Variant(10,ua.VariantType.UInt16))
        cls.count=obj.add_variable(ns,'CartonsPacked',ua.Variant(7,ua.VariantType.UInt32))
        cls.permit=obj.add_variable(ns,'Permit',True)
        cls.start=obj.add_variable(ns,'Start',False); cls.start.set_writable()
        cls.enable=obj.add_variable(ns,'Enable',False); cls.enable.set_writable()
        cls.server.start()

    @classmethod
    def tearDownClass(cls):cls.server.stop()

    def setUp(self):
        self.state.set_value(self.ua.Variant(10,self.ua.VariantType.UInt16))
        self.start.set_value(False); self.enable.set_value(False)
        self.events=queue.Queue(); self.link=OpcLink(self.events)
        self.config=json.loads(Path(__file__).with_name('config.json').read_text())
        self.config['endpoint']=self.endpoint
        self.config['poll_ms']=50
        self.config['nodes']={key:'' for key in self.config['nodes']}
        self.config['nodes'].update(state=self.state.nodeid.to_string(),count=self.count.nodeid.to_string(),
                                    permit=self.permit.nodeid.to_string(),start=self.start.nodeid.to_string(),
                                    enable=self.enable.nodeid.to_string())

    def tearDown(self):
        self.link.disconnect()
        if self.link.thread:self.link.thread.join(5)

    def wait_event(self,kind,timeout=6):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:k,p=self.events.get(timeout=0.1)
            except queue.Empty:continue
            if k==kind:return p
            if k=='error' and kind!='error':self.fail(p)
        self.fail('Missing event: '+kind)

    def test_read_write_pulse_and_browse(self):
        self.link.connect(self.config)
        self.wait_event('connected')
        snap=self.wait_event('snapshot')
        self.assertEqual((snap['state'],snap['count'],snap['permit']),(10,7,True))
        self.link.write('enable',True); self.wait_event('written')
        self.assertTrue(self.enable.get_value())
        self.link.write('start',True,pulse=True)
        # Verify that the pulse actually rises and subsequently falls.
        deadline=time.monotonic()+3
        high=False
        while time.monotonic()<deadline:
            if self.start.get_value():high=True;break
            time.sleep(0.01)
        self.assertTrue(high)
        self.wait_event('written')
        self.assertFalse(self.start.get_value())
        self.link.browse(); token,rows,truncated=self.wait_event('browse')
        self.assertEqual(token,'root'); self.assertTrue(rows)
        self.assertTrue(any(row[2]=='Object' for row in rows))
        with self.assertRaises(ValueError):self.link.write('permit',True)

    def test_bad_quality_disconnects_without_false_zero(self):
        self.link.connect(self.config); self.wait_event('snapshot')
        value=self.ua.DataValue(self.ua.Variant(10,self.ua.VariantType.UInt16))
        value.StatusCode=self.ua.StatusCode(self.ua.StatusCodes.BadNoCommunication)
        self.state.set_attribute(self.ua.AttributeIds.Value,value)
        error=self.wait_event('error')
        self.assertIn('BadNoCommunication',error)
        self.wait_event('disconnected')
        self.assertFalse(self.link.connected)

    def test_invalid_mapping_reports_error(self):
        self.config['nodes']['state']='ns=42;s=does-not-exist'
        self.link.connect(self.config)
        error=self.wait_event('error')
        self.assertIn('BadNodeIdUnknown',error)

    def test_explicit_namespace_uri_resolves_changed_indexes(self):
        self.config['namespace_uri']='urn:his:gui:test'
        from opcua import ua
        for key,value in list(self.config['nodes'].items()):
            if value:
                nodeid=ua.NodeId.from_string(value)
                self.config['nodes'][key]=ua.NodeId(nodeid.Identifier,99,nodeid.NodeIdType).to_string()
        self.link.connect(self.config)
        self.assertEqual(self.wait_event('snapshot')['state'],10)

    def test_unknown_namespace_uri_reports_configuration_error(self):
        self.config['namespace_uri']='urn:missing:test'
        self.link.connect(self.config)
        self.assertIn('namespace URI',self.wait_event('error'))


if __name__=='__main__':unittest.main()
