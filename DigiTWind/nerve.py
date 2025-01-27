# Copyright 2023 - Yuksel Rudy Alkarem

import pandas as pd
import numpy as np
import multiprocessing as mp
from multiprocessing import Process, Manager
from ROSCO_toolbox.ofTools.fast_io.FAST_reader import InputReader_OpenFAST
from ROSCO_toolbox.ofTools.fast_io.FAST_writer import InputWriter_OpenFAST
from ROSCO_toolbox import control_interface as ROSCO_ci
from ROSCO_toolbox.control_interface import turbine_zmq_server
from ROSCO_toolbox.inputs.validation import load_rosco_yaml
from ROSCO_toolbox.utilities import run_openfast
from ROSCO_toolbox.utilities import read_DISCON, write_DISCON
from ROSCO_toolbox import turbine as ROSCO_turbine
from ROSCO_toolbox import controller as ROSCO_controller
import os
import time

class NervePhysical:
    def __init__(self, filename):
        self.data = pd.read_csv(filename)

    def get_data(self):
        return self.data

    def scale_data(self, df, channel_info, zero_drift):
        tnames = channel_info['TNAME']
        units = channel_info['UNIT']
        scale = channel_info['scale']  # now scale is a common value for all channels

        for tname, unit in zip(tnames, units):
            if unit == 's':
                df[tname] *= np.sqrt(scale)
            elif unit == 'm':
                df[tname] *= scale
            # For 'deg' or any other unit, do nothing

        # If zero drift is to be considered, apply it to all channels except 'Time'
        if zero_drift:
            for tname in tnames:
                if tname != 'Time':
                    df[tname] -= df[tname].mean()  # Subtract mean to make it zero-mean

        return df


class NerveVirtual:
    def __init__(self, twin_rate):
        self.twin_rate = twin_rate
        self.manager   = Manager()
        self.shared_dict = self.manager.dict()
        self.shared_max_time = self.manager.Value('d', 0.0)

    def update_twin_rate(self, param_filename, turbine_params, turbine_name, controller_params):
        # Read, update, and write DISCON file (turbine is dummy except for the name,
        # controller is dummy here)
        DISCON_in = read_DISCON(param_filename)
        DISCON_in["ZMQ_Mode"] = 1
        DISCON_in["ZMQ_UpdatePeriod"] = self.twin_rate
        turbine = ROSCO_turbine.Turbine(turbine_params)
        turbine.TurbineName = turbine_name
        controller = ROSCO_controller.Controller(controller_params)
        write_DISCON(turbine, controller, param_file=param_filename, rosco_vt=DISCON_in)

    def change_model_setup(self, fastfile, OF_filename, f_list,
                       v_list, des_v_list):
        fastin = InputReader_OpenFAST()
        fastout = InputWriter_OpenFAST()
        fastin.FAST_InputFile     = fastfile
        fastin.FAST_directory     = OF_filename
        fastout.FAST_namingOut    = fastin.FAST_InputFile[:-4]
        fastout.FAST_runDirectory = fastin.FAST_directory

        # read input files
        self.read_openfast_files(fastin, f_list)
        fastout.fst_vt = fastin.fst_vt

        # backup clean variables
        clean_variables = self.backup_clean_variables(f_list, v_list, fastin)

        # change variables
        for file, variable, desired in zip(f_list, v_list, des_v_list):
            if file in fastout.fst_vt and variable in fastout.fst_vt[file]:
                fastout.fst_vt[file][variable] = desired
            else:
                print(f"Error: Invalid file or variable specified: {file}, {variable}")


        # write output files
        self.write_openfast_files(fastout, f_list)
        return clean_variables, fastout

    def restore_model_setup(self, f_list, v_list, clean_variables, of_file):
        for file_name, variable, clean_value in zip(f_list, v_list, clean_variables):
            if file_name in of_file.fst_vt and variable in of_file.fst_vt[file_name]:
                of_file.fst_vt[file_name][variable] = clean_value
            else:
                print(f"Error: Invalid file or variable specified: {file_name}, {variable}")

        # write output files
        self.write_openfast_files(of_file, f_list)

    def backup_clean_variables(self, f_list, v_list, of_file):
        clean_variables = []
        for file_name, variable in zip(f_list, v_list):
            if file_name in of_file.fst_vt and variable in of_file.fst_vt[file_name]:
                clean_value = of_file.fst_vt[file_name][variable]
                clean_variables.append(clean_value)
            else:
                print(f"Error: Invalid file or variable specified: {file_name}, {variable}")

        return clean_variables

    @staticmethod
    def read_openfast_files(of_file, f_list):
        of_file.read_MainInput()
        for file in f_list:
            if file == 'ElastoDyn':
                ed_file = os.path.join(of_file.FAST_directory, of_file.fst_vt['Fst']['EDFile'])
                of_file.read_ElastoDyn(ed_file)
            elif file == 'HydroDyn':
                hd_file = os.path.join(of_file.FAST_directory, of_file.fst_vt['Fst']['HydroFile'])
                of_file.read_HydroDyn(hd_file)
            elif file == 'ServoDyn':
                of_file.read_ServoDyn()
            else:
                print(f"Error: Undefined input file specified: {file}")

    @staticmethod
    def write_openfast_files(of_file, f_list):
        for file in f_list:
            if file == 'Fst':
                of_file.write_MainInput()
            elif file == 'ElastoDyn':
                of_file.write_ElastoDyn()
            elif file == 'HydroDyn':
                of_file.write_HydroDyn()
            elif file == 'ServoDyn':
                of_file.write_ServoDyn()
            else:
                print(f"Error: Undefined output file specified: {file}")


    def run_virtual(self, OF_filename, fastcall, fastfile, lib_name, param_filename):
        # Load controller library
        controller_int = ROSCO_ci.ControllerInterface(lib_name, param_filename=param_filename)
        start_time = time.time()
        run_openfast(
            OF_filename,
            fastcall=fastcall,
            fastfile=fastfile,
            chdir=True
        )
        end_time = time.time()
        elapsed_time = end_time - start_time
        print(f"Time taken to run the process: {elapsed_time} seconds")
    def run_zmq(self):
        self.connect_zmq = True
        self.s = turbine_zmq_server(network_address="tcp://*:5555", timeout=600.0, verbose=False)
        while self.connect_zmq:
            #  Get latest measurements from ROSCO
            self.measurements = self.s.get_measurements()
            yaw_setpoint = 0.0
            self.s.send_setpoints(nacelleHeading=yaw_setpoint)
            self.shared_dict[self.measurements['Time']] = self.measurements  # Store measurements at each time step
            self.shared_max_time.value = self.measurements['Time']
            if self.measurements['iStatus'] == -1:
                self.connect_zmq = False
                self.s._disconnect()

    def output_manager(self, output_folder, output_file, of_file):
        # Moving output files to output directory (supports: .out, .outb, .MD.out)
        outdir = os.path.join(output_folder, output_file)

        if not os.path.isdir(outdir):
          os.makedirs(outdir)

        outname = of_file.FAST_namingOut + '.out'
        outbname = of_file.FAST_namingOut + '.outb'
        MDoutname = of_file.FAST_namingOut + '.MD.out'
        if os.path.isfile(os.path.join(of_file.FAST_runDirectory,outname)):
          os.rename(os.path.join(of_file.FAST_runDirectory,outname), os.path.join(outdir,outname))

        if os.path.isfile(os.path.join(of_file.FAST_runDirectory,outbname)):
          os.rename(os.path.join(of_file.FAST_runDirectory,outbname), os.path.join(outdir,outbname))

        if os.path.isfile(os.path.join(of_file.FAST_runDirectory,MDoutname)):
          os.rename(os.path.join(of_file.FAST_runDirectory,MDoutname), os.path.join(outdir,MDoutname))

class NerveLinear:
    def __init__(self,Lin_Hydro,Lin_Platform,Lin_Outputs)
        self.Lin_Hydro = Lin_Hydro
        self.LinPlatform = Lin_Platform
        self.Lin_Outputs = Lin_Outputs

    def readFASTLinear(filepath):
        
