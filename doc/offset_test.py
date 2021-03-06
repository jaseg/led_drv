#!/usr/bin/env python3

import time
import statistics
import sqlite3
from olsndot import Olsndot, Driver
from datetime import datetime

from pyBusPirateLite import Buspirate

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('run_name', nargs='?', default='auto')
    parser.add_argument('olsndot_port', nargs='?', default='/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0')
    parser.add_argument('buspirate_port', nargs='?', default='/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AD01W1RF-if00-port0')
    parser.add_argument('-c', '--channels', nargs='?', default='auto', help='olsndot channels to test, format: 0-3,5,7,8-10')
    parser.add_argument('-d', '--database', default='results.sqlite3', help='sqlite3 database file to store results in')
    parser.add_argument('-m', '--mac', type=int, default=0xDEBE10BB, help='olsndot MAC address')
    parser.add_argument('-w', '--wait', type=float, default=0.1, help='time to wait between samples in seconds')
    parser.add_argument('-o', '--oversample', type=int, default=16, help='oversampling ratio')
    parser.add_argument('-b', '--bits', type=int, default=None, help='number of bits to sample')
    args = parser.parse_args()

    db = sqlite3.connect(args.database)
    db.execute("""
        CREATE TABLE IF NOT EXISTS runs (
                run_id INTEGER PRIMARY KEY,
                name TEXT,
                comment TEXT,
                uut_mac TEXT, -- hex-string formatted 32-bit mac of the uut
                timestamp REAL -- unix timestamp in fractional seconds
                )""")
    db.execute("""
        CREATE TABLE IF NOT EXISTS measurements (
                measurement_id INTEGER PRIMARY KEY,
                run_id INTEGER,
                channel INTEGER,
                duty_cycle REAL, -- setpoint duty cycle as a float between 0.0 and 1.0
                voltage REAL, -- volts
                voltage_stdev REAL, -- volts
                timestamp REAL, -- unix timestamp in fractional seconds
                FOREIGN KEY (run_id) REFERENCES runs)""")

    bp = Buspirate(args.buspirate_port)
    bp.power_on = True

    uut = Olsndot(args.mac)
    d = Driver(args.olsndot_port, devices=[uut])
    print('Connected to uut:', uut)

    run_name = args.run_name
    if not str.isnumeric(args.run_name[-1]):
        names = [ n[len(run_name):] for n, in db.execute(
            'SELECT name FROM runs WHERE name LIKE ?||"%"', (run_name,)).fetchall() ]
        names.append('0') # in case we get no results
        run_name += str(1+max(int(n) if str.isnumeric(n) else 0 for n in names))
    with db:
        cur = db.cursor()
        cur.execute('INSERT INTO runs(name, uut_mac, timestamp) VALUES (?, ?, ?)',
                (run_name, args.mac, time.time()))
        run_id = cur.lastrowid

    nbits = args.bits if args.bits is not None else uut.nbits

    def parse_channels(channels):
        for spec in channels.split(','):
            if str.isnumeric(spec):
                yield int(spec)
            else:
                low, high = spec.split('-')
                yield from range(int(low), int(high)+1)
    if args.channels == 'auto':
        for i in range(uut.nchannels):
            fb = [0]*uut.nchannels
            fb[i] = 0xffff;
            uut.send_framebuf(fb)
            time.sleep(0.2)
            if bp.adc_value > 0.5:
                break;
        else:
            raise ValueError('Cannot find active channel')
        channels = [i]
    else:
        channels = list(parse_channels(args.channels))

    print('Starting run {} "{}" at {:%y-%m-%d %H:%M:%S:%f}'.format(run_id, run_name, datetime.now()))
    print('mac={:08x} channels={}'.format(args.mac, ','.join('{:02d}'.format(ch) for ch in channels)))
    print('[measurement id] " " [hex setpoint value] "(" [float duty cycle] ")" " " [reading (V)]')

    # zero cal
    uut.send_framebuf([0]*uut.nchannels)
    time.sleep(args.wait)
    readings = [ bp.adc_value for _ in range(args.oversample) ]
    zero_mean, stdev = statistics.mean(readings), statistics.stdev(readings)
    cur.execute('''
        INSERT INTO measurements (
                run_id, channel, duty_cycle, voltage, voltage_stdev, timestamp
            ) VALUES (?, -1, 0, ?, ?, ?)''',
            (run_id, zero_mean, stdev, time.time()))
    print('Zero cal: {:5.4f}V stdev={:5.4f}V'.format(zero_mean, stdev))

    for ch in channels:
        for i in range(nbits):
            fb = [0]*uut.nchannels
            val = 1<<i
            duty_cycle = val/(2**uut.nbits)
            extra_shift = 16-uut.nbits
            val <<= extra_shift

            fb[ch] = val
            uut.send_framebuf(fb)
            
            time.sleep(args.wait)
            readings = [ bp.adc_value for _ in range(args.oversample) ]
            mean, stdev = statistics.mean(readings), statistics.stdev(readings)

            with db:
                cur = db.cursor()
                cur.execute('''
                    INSERT INTO measurements (
                            run_id, channel, duty_cycle, voltage, voltage_stdev, timestamp
                        ) VALUES (?, ?, ?, ?, ?, ?)''',
                        (run_id, ch, duty_cycle, mean, stdev, time.time()))
                print('{:08d} ch={} {:04x}({:6.5f}): {:5.4f} stdev {:5.4f}'.format(
                    cur.lastrowid, ch, val, duty_cycle, mean-zero_mean, stdev))

    uut.send_framebuf([0]*uut.nchannels)
    bp.power_on = False

