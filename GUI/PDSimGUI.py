# -*- coding: latin-1 -*-

#Imports from wx package
import wx
from wx.lib.mixins.listctrl import CheckListCtrlMixin, ColumnSorterMixin, ListCtrlAutoWidthMixin
from wx.lib.embeddedimage import PyEmbeddedImage
import wx.lib.agw.pybusyinfo as PBI
from wx.lib.wordwrap import wordwrap
wx.SetDefaultPyEncoding('latin-1') 

#Provided by python
import os, sys
import codecs
from operator import itemgetter
from math import pi
from Queue import Queue, Empty
from multiprocessing import Process, Pipe, freeze_support, cpu_count, allow_connection_pickling
from threading import Thread
import time
import textwrap
import cPickle
from ConfigParser import SafeConfigParser
import StringIO
import warnings

#Other packages that are required
import numpy as np
import CoolProp.State as CPState

#PDSim imports
from PDSim.recip.core import Recip
from PDSim.scroll.core import Scroll
from PDSimLoader import RecipBuilder, ScrollBuilder
from PDSim.plot.plots import PlotNotebook
import PDSim

#PDSim GUI imports
import pdsim_panels
import pdsim_plugins
import recip_panels
import scroll_panels
import default_configs 

class InfiniteList(object):
    """
    Creates a special list where removing an element just puts it back at the end of the list
    """
    def __init__(self, values):
        """
        
        Parameters
        ----------
        values : list
        """
        self.values = values
        
    def pop(self):
        """
        Return the first element, then put the first element back at the end of the list
        """
        val1 = self.values[0]
        self.values.pop(0)
        self.values.append(val1)
        return val1
       
class RedirectText2Pipe(object):
    """
    An text output redirector
    """
    def __init__(self, pipe_inlet):
        self.pipe_inlet = pipe_inlet
    def write(self, string):
        self.pipe_inlet.send(string)
    def flush(self):
        return None

class Run1(Process):
    """
    A multiprocessing Process class that actually runs one simulation
    
    Everything within the run() function MUST!! be picklable.  This is a major
    headache but required in order to fork a process of python for the simulation.
    
    """
    def __init__(self, pipe_std, pipe_abort, pipe_results, simulation):
        Process.__init__(self)
        #Keep local variables that point to the pipes
        self.pipe_std = pipe_std
        self.pipe_abort = pipe_abort
        self.pipe_results = pipe_results
        #Local pointer to simulation
        self.sim = simulation
        #Reset the abort flag at instantiation
        self._want_abort = False

    def run(self):
        # Any stdout or stderr output will be redirected to a pipe for passing
        # back to the GUI.  Pipes must be used because they are picklable and
        # otherwise the text output will not show up anywhere
        redir = RedirectText2Pipe(self.pipe_std)
        sys.stdout = redir
        sys.stderr = redir
        
        #Set solver parameters from the GUI
        OneCycle = self.sim.OneCycle if hasattr(self.sim,'OneCycle') else False
        plot_every_cycle = self.sim.plot_every_cycle if hasattr(self.sim,'plot_every_cycle')  else False
            
        # These parameters are common to all compressor types, so put them all
        # in one dictionary and unpack it into each function call
        commons = dict(key_inlet='inlet.1',
                       key_outlet='outlet.2',
                       endcycle_callback=self.sim.endcycle_callback,
                       heat_transfer_callback=self.sim.heat_transfer_callback,
                       lump_energy_balance_callback = self.sim.lump_energy_balance_callback, 
                       OneCycle=OneCycle,
                       solver_method = self.sim.cycle_integrator_type,
                       pipe_abort = self.pipe_abort,
                       plot_every_cycle = plot_every_cycle
                       )
        
        if isinstance(self.sim, Recip):
            self.sim.precond_solve(UseNR = True,
                                   valves_callback =self.sim.valves_callback,
                                   **commons
                                   )
        elif isinstance(self.sim, Scroll):
            self.sim.precond_solve(UseNR = False, #Use Newton-Raphson ND solver to determine the initial state if True
                                   step_callback = self.sim.step_callback,                                   
                                   **commons
                                   )
        else:
            raise TypeError
        
        #Delete a few items that cannot pickle properly
        if hasattr(self.sim,'pipe_abort'):
            del self.sim.pipe_abort
            del self.sim.FlowStorage
            del self.sim.Abort #Can't pickle because it is a pointer to a bound method
        
        if not self.sim._want_abort:
            #Send simulation result back to calling thread
            self.pipe_results.send(self.sim)
            print 'Sent simulation back to calling thread'
            #Wait for an acknowledgment of receipt
            while not self.pipe_results.poll():
                time.sleep(0.1)
                #Check that you got the right acknowledgment key back
                ack_key = self.pipe_results.recv()
                if not ack_key == 'ACK':
                    raise KeyError
                else:
                    print 'Acknowledgment of receipt accepted'
                    break
        else:
            print 'Acknowledging completion of abort'
            self.pipe_abort.send('ACK')
        
class WorkerThreadManager(Thread):
    """
    This manager thread creates all the threads that run.  It checks how many processors are available and runs Ncore-1 processes
    
    Runs are consumed from the simulations one at a time
    """
    def __init__(self, target, simulations, stdout_targets, args = None, done_callback = None, 
                 add_results = None, Ncores = None, main_stdout = None):
        Thread.__init__(self)
        self.target = target
        self.args = args if args is not None else tuple()
        self.done_callback = done_callback
        self.add_results = add_results
        self.simulations = simulations
        self.stdout_targets = stdout_targets
        self.threadsList = []
        self.stdout_list = InfiniteList(stdout_targets)
        self.main_stdout = main_stdout
        if Ncores is None:
            self.Ncores = cpu_count()-1
        else:
            self.Ncores = Ncores
        if self.Ncores<1:
            self.Ncores = 1
        wx.CallAfter(self.main_stdout.WriteText, "Want to run "+str(len(self.simulations))+" simulations in batch mode; "+str(self.Ncores)+' cores available for computation\n')
            
    def run(self):
        #While simulations left to be run or computation is not finished
        while self.simulations or self.threadsList:
            
            #Add a new thread if possible (leave one core for main GUI)
            if len(self.threadsList) < self.Ncores and self.simulations:
                #Get the next simulation to be run as a tuple
                simulation = (self.simulations.pop(0),)
                #Start the worker thread
                t = RedirectedWorkerThread(self.target, self.stdout_list.pop(), 
                                          args = simulation+self.args, 
                                          done_callback = self.done_callback, 
                                          add_results = self.add_results,
                                          main_stdout = self.main_stdout)
                t.daemon = True
                t.start()
                self.threadsList.append(t)
                wx.CallAfter(self.main_stdout.AppendText, 'Adding thread;' + str(len(self.threadsList)) + ' threads active\n') 
            
            for _thread in reversed(self.threadsList):
                if not _thread.is_alive():
                    wx.CallAfter(self.main_stdout.AppendText, 'Joining zombie thread\n')
                    _thread.join()
                    self.threadsList.remove(_thread)
                    wx.CallAfter(self.main_stdout.AppendText, 'Thread finished; now '+str(len(self.threadsList))+ ' threads active\n')
            
            time.sleep(2.0)
    
    def abort(self):
        """
        Pass the message to quit to all the threads; don't run any that are queued
        """
        dlg = wx.MessageDialog(None,"Are you sure you want to kill the current runs?",caption ="Kill Batch?",style = wx.OK|wx.CANCEL)
        if dlg.ShowModal() == wx.ID_OK:
            message = "Aborting in progress, please wait..."
            busy = PBI.PyBusyInfo(message, parent = None, title = "Aborting")
            #Empty the list of simulations to run
            self.simulations = []
            
            for _thread in self.threadsList:
                #Send the abort signal
                _thread.abort()
#                #Wait for it to finish up
#                _thread.join()
            del busy
            
        dlg.Destroy()
        
class RedirectedWorkerThread(Thread):
    """Worker Thread Class."""
    def __init__(self, target, stdout_target = None,  args = None, kwargs = None, done_callback = None, add_results = None, main_stdout = None):
        """Init Worker Thread Class."""
        Thread.__init__(self)
        self.target_ = target
        self.stdout_target_ = stdout_target
        self.args_ = args if args is not None else tuple()
        self._want_abort = False
        self.done_callback = done_callback
        self.add_results = add_results
        self.main_stdout = main_stdout
        
    def run(self):
        """
        In this function, actually run the process and pull any output from the 
        pipes while the process runs
        """
        sim = None
        pipe_outlet, pipe_inlet = Pipe(duplex = False)
        pipe_abort_outlet, pipe_abort_inlet = Pipe(duplex = True)
        pipe_results_outlet, pipe_results_inlet = Pipe(duplex = True)

        p = Run1(pipe_inlet, pipe_abort_outlet, pipe_results_inlet, self.args_[0])
        p.daemon = True
        p.start()
        
        while p.is_alive():
                
            #If the manager is asked to quit
            if self._want_abort == True:
                #Tell the process to abort, passes message to simulation run
                pipe_abort_inlet.send(True)
                #Wait until it acknowledges the kill by sending back 'ACK'
                while not pipe_abort_inlet.poll():
                    time.sleep(0.1)
#                   #Collect all display output from process while you wait
                    while pipe_outlet.poll():
                        wx.CallAfter(self.stdout_target_.AppendText, pipe_outlet.recv())
                        
                abort_flag = pipe_abort_inlet.recv()
                if abort_flag == 'ACK':
                    break
                else:
                    raise ValueError('abort pipe should have received a value of "ACK"')
                
            #Collect all display output from process
            while pipe_outlet.poll():
                wx.CallAfter(self.stdout_target_.AppendText, pipe_outlet.recv())
            time.sleep(0.5)    
            
            #Get back the results from the simulation process if they are waiting
            if pipe_results_outlet.poll():
                sim = pipe_results_outlet.recv()
                pipe_results_outlet.send('ACK')
        
        #Flush out any remaining stuff left in the pipe after process ends
        while pipe_outlet.poll():
            wx.CallAfter(self.stdout_target_.AppendText, pipe_outlet.recv())
        
        
                    
        if self._want_abort == True:
            print self.name+": Process has aborted successfully"
        else:
            wx.CallAfter(self.stdout_target_.AppendText, self.name+": Process is done")
            if sim is not None:
                #Get a unique identifier for the model run for pickling purposes
                home = os.getenv('USERPROFILE') or os.getenv('HOME')
                temp_folder = os.path.join(home,'.pdsim-temp')
                try:
                    os.mkdir(temp_folder)
                except OSError:
                    pass
                except WindowsError:
                    pass
                
                identifier = 'PDSim recip ' + time.strftime('%Y-%m-%d-%H-%M-%S')+'_t'+self.name.split('-')[1]
                file_path = os.path.join(temp_folder, identifier + '.mdl')
                hdf5_path = os.path.join(temp_folder, identifier + '.h5')
                
                print 'Wrote pickled file to', file_path
                print 'Wrote hdf5 file to', hdf5_path
                if not os.path.exists(file_path):
                    fName = file_path
                else:
                    i = 65
                    def _file_path(i):
                        return os.path.join(temp_folder, identifier + str(chr(i)) + '.mdl')
                    
                    if os.path.exists(_file_path(i)):
                        while os.path.exists(_file_path(i)):
                            i += 1
                        i -= 1
                    fName = _file_path(i)
                
                #Write it to a binary pickled file for safekeeping
                fp = open(fName, 'wb')
                #del sim.FlowStorage
                print "Warning: removing FlowStorage since it doesn't pickle properly"
                cPickle.dump(sim, fp, protocol = -1)
                fp.close()
                
                from plugins.HDF5_plugin import HDF5Writer
                HDF5 = HDF5Writer()
                HDF5.write_to_file(sim, hdf5_path)
                
                "Send the data back to the GUI"
                wx.CallAfter(self.done_callback, sim)
            else:
                print "Didn't get any simulation data"
        return 1
        
    def abort(self):
        """abort worker thread."""
        wx.CallAfter(self.main_stdout.WriteText, self.name + ': Thread readying for abort\n')
        # Method for use by main thread to signal an abort
        self._want_abort = True
    
class InputsToolBook(wx.Toolbook):
    """
    The toolbook that contains the pages with input values
    """
    def __init__(self,parent,configfile,id=-1):
        wx.Toolbook.__init__(self, parent, -1, style=wx.BK_LEFT)
        il = wx.ImageList(32, 32)
        indices=[]
        for imgfile in ['Geometry.png',
                        'MassFlow.png',
                        'MechanicalLosses.png',
                        'StatePoint.png']:
            ico_path = os.path.join('ico',imgfile)
            indices.append(il.Add(wx.Image(ico_path,wx.BITMAP_TYPE_PNG).ConvertToBitmap()))
        self.AssignImageList(il)
        
        parser = SafeConfigParser()
        parser.optionxform = unicode
        
        Main = wx.GetTopLevelParent(self)
        if Main.SimType == 'recip':
            # Make the recip panels.  Name should be consistent with configuration 
            # file section heading
            self.panels=(recip_panels.GeometryPanel(self, 
                                                    configfile,
                                                    name='GeometryPanel'),
                         recip_panels.MassFlowPanel(self,
                                                    configfile,
                                                    name='MassFlowPanel'),
                         recip_panels.MechanicalLossesPanel(self,
                                                            configfile,
                                                            name='MechanicalLossesPanel'),
                         pdsim_panels.StateInputsPanel(self,
                                                       configfile,
                                                       name='StatePanel')
                         )
            
        elif Main.SimType == 'scroll':
            # Make the scroll panels.  Name should be consistent with configuration 
            # file section heading
            self.panels=(scroll_panels.GeometryPanel(self, 
                                                    configfile,
                                                    name='GeometryPanel'),
                         scroll_panels.MassFlowPanel(self,
                                                    configfile,
                                                    name='MassFlowPanel'),
                         scroll_panels.MechanicalLossesPanel(self,
                                                    configfile,
                                                    name='MechanicalLossesPanel'),
                         pdsim_panels.StateInputsPanel(self,
                                                   configfile,
                                                   name='StatePanel')
                         )
        
        
        for Name, index, panel in zip(['Geometry','Mass Flow - Valves','Mechanical','State Points'],indices,self.panels):
            self.AddPage(panel,Name,imageId=index)
            
    def set_params(self, simulation):
        """
        Pull all the values out of the child panels, using the values in 
        self.items and the function post_set_params if the panel implements
        it
        """
        for panel in self.panels:
            panel.set_params(simulation)
    
    def post_set_params(self, simulation):
        for panel in self.panels:
            if hasattr(panel,'post_set_params'):
                panel.post_set_params(simulation)
                
    def collect_parametric_terms(self):
        """
        Collect all the terms to be added to parametric table
        """
        items = [] 
        #get a list of the panels that subclass PDPanel
        panels = [panel for panel in self.Children if isinstance(panel,pdsim_panels.PDPanel)]
        for panel in panels:
            if hasattr(panel,'items'):
                items += panel.items
            more_items = panel.get_additional_parametric_terms()
            if more_items is not None:
                items += more_items
        return items
    
    def apply_additional_parametric_terms(self, attrs, vals, items):
        """
        Apply parametric terms for each panel
        
        Parameters
        ----------
        attrs : list of strings
            Attributes that are included in the parametric table
        vals : the values corresponding to each attr in attrs
        items : the list of dictionaries of item data in para table
        
        """
        #get a list of the panels that subclass PDPanel
        panels = [panel for panel in self.Children if isinstance(panel,pdsim_panels.PDPanel)]
        for panel in panels:
            #Collect all the additional terms that apply to the panel
            panel_items = [item for item in items if 'parent' in item and item['parent'] == panel]
            # Returns the remaining attrs, vals
            attrs, vals = panel.apply_additional_parametric_terms(attrs, vals, panel_items)
        
        return attrs, vals
    
    def collect_output_terms(self):
        terms = []
        for panel in self.panels:
            if hasattr(panel,'collect_output_terms'):
                terms += panel.collect_output_terms()
        return terms
    
class IntegratorChoices(wx.Choicebook):
    def __init__(self, parent, **kwargs):
        wx.Choicebook.__init__(self, parent, id = wx.ID_ANY, **kwargs)
    
        # Build the choicebook items
        self.pageEuler=wx.Panel(self)
        self.AddPage(self.pageEuler,'Simple Euler')
        tt = 'Number of steps to be taken by the Euler solver per cycle'
        self.EulerNlabel, self.EulerN = pdsim_panels.LabeledItem(self.pageEuler,
                                                  label="Number of Steps [-]",
                                                  value='7000',
                                                  tooltip = tt)
        sizer=wx.FlexGridSizer(cols=2,hgap=3,vgap=3)
        sizer.AddMany([self.EulerNlabel, self.EulerN])
        self.pageEuler.SetSizer(sizer)
        
        self.pageHeun=wx.Panel(self)
        self.AddPage(self.pageHeun,'Heun')
        tt ='Number of steps to be taken by the Heun solver per cycle'
        self.HeunNlabel, self.HeunN = pdsim_panels.LabeledItem(self.pageHeun,
                                                  label="Number of Steps [-]",
                                                  value='7000',
                                                  tooltip = tt)
        sizer=wx.FlexGridSizer(cols=2,hgap=3,vgap=3)
        sizer.AddMany([self.HeunNlabel, self.HeunN])
        self.pageHeun.SetSizer(sizer)

        tt = """The maximum allowed absolute error per step of the solver"""
        self.pageRK45=wx.Panel(self)
        self.AddPage(self.pageRK45,'Adaptive Runge-Kutta 4/5')
        self.RK45_eps_label, self.RK45_eps = pdsim_panels.LabeledItem(self.pageRK45,
                                                  label="Maximum allowed error per step [-]",
                                                  value='1e-8',
                                                  tooltip = tt)
        sizer=wx.FlexGridSizer(cols=2,hgap=3,vgap=3)
        sizer.AddMany([self.RK45_eps_label, self.RK45_eps])
        self.pageRK45.SetSizer(sizer)
    
    def set_sim(self, simulation):
        
        if self.GetSelection() == 0:
            simulation.cycle_integrator_type = 'Euler'
            simulation.EulerN = int(self.EulerN.GetValue())
        elif self.GetSelection() == 1:
            simulation.cycle_integrator_type = 'Heun'
            simulation.HeunN = int(self.HeunN.GetValue())
        else:
            simulation.cycle_integrator_type = 'RK45'
            simulation.RK45_eps = float(self.RK45_eps.GetValue())

    def set_from_string(self, config_string):
        """
        config_string will be something like Cycle,Euler,7000 or Cycle,RK45,1e-8
        """
        #Chop off the Cycle,
        config_string = config_string.split(',',1)[1]
        
        SolverType, config = config_string.split(',',1)
        if SolverType == 'Euler':
            self.SetSelection(0)
            self.EulerN.SetValue(config)
        elif SolverType == 'Heun':
            self.SetSelection(1)
            self.HeunN.SetValue(config)
        elif SolverType == 'RK45':
            self.SetSelection(2)
            self.RK45_eps.SetValue(config)
        
    def save_to_string(self):
        if self.GetSelection() == 0:
            return 'Cycle = Cycle,Euler,'+self.EulerN.GetValue()
        elif self.GetSelection() == 1:
            return 'Cycle = Cycle,Heun,'+self.HeunN.GetValue()
        else:
            return 'Cycle = Cycle,RK45,'+self.RK45_eps.GetValue()
        
class SolverInputsPanel(pdsim_panels.PDPanel):
    def __init__(self, parent, configfile,**kwargs):
        pdsim_panels.PDPanel.__init__(self, parent, **kwargs)
    
        self.IC = IntegratorChoices(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        #Loads all the parameters from the config file (case-sensitive)
        self.configdict, self.descdict = self.get_from_configfile('SolverInputsPanel')
        
        self.items = [dict(attr = 'eps_cycle')]
        
        sizer.Insert(0, self.IC)
        sizer.AddSpacer(10)
        
        sizer_tols = wx.FlexGridSizer(cols = 2)        
        self.ConstructItems(self.items, sizer_tols, self.configdict, self.descdict)
        sizer.Add(sizer_tols)
        
        sizer_advanced = wx.FlexGridSizer(cols = 1)
        
        sizer.Add(wx.StaticText(self,label='Advanced/Debug options'))
        sizer.Add(wx.StaticLine(self, -1, (25, 50), (300,1)))
        self.OneCycle = wx.CheckBox(self,
                                    label = "Just run one cycle - not the full solution")
        self.plot_every_cycle = wx.CheckBox(self,
                                            label = "Open the plots after each cycle (warning - very annoying but good for debug)")
        sizer_advanced.AddMany([self.OneCycle,
                                self.plot_every_cycle])
        sizer.Add(sizer_advanced)
        sizer.Layout()
        
    def post_get_from_configfile(self, key, config_string):
        """
        Build the integrator chooser 
        
        This function will be called by PDPanel.get_from_configfile
        """
        if key == 'Cycle':
            self.IC.set_from_string(config_string)
        
    def post_prep_for_configfile(self):
        return self.IC.save_to_string()+'\n'
        
    def post_set_params(self, simulation):
        self.IC.set_sim(simulation)
        simulation.OneCycle = self.OneCycle.IsChecked()
        simulation.plot_every_cycle = self.plot_every_cycle.IsChecked()
            
    def supply_parametric_term(self):
        pass
        
class SolverToolBook(wx.Toolbook):
    def __init__(self,parent,configfile,id=-1):
        wx.Toolbook.__init__(self, parent, -1, style=wx.BK_LEFT)
        il = wx.ImageList(32, 32)
        indices=[]
        for imgfile in ['Geometry.png','MassFlow.png']:
            ico_path = os.path.join('ico',imgfile)
            indices.append(il.Add(wx.Image(ico_path,wx.BITMAP_TYPE_PNG).ConvertToBitmap()))
        self.AssignImageList(il)
        
        items = self.Parent.InputsTB.collect_parametric_terms()
        #Make the panels.  Name should be consistent with configuration file
        pane1=SolverInputsPanel(self, configfile, name = 'SolverInputsPanel')
        pane2=pdsim_panels.ParametricPanel(self, configfile, items, name='ParametricPanel')
        self.panels=(pane1,pane2)
        
        for Name,index,panel in zip(['Params','Parametric'],indices,self.panels):
            self.AddPage(panel, Name, imageId=index)
            
    def set_params(self,simulat):
        for panel in self.panels:
            panel.set_params(simulat)
            
    def post_set_params(self, simulat):
        for panel in self.panels:
            if hasattr(panel,'post_set_params'):
                panel.post_set_params(simulat)
    
    def collect_parametric_terms(self):
        """
        Collect parametric terms from the panels in this toolbook
        """
        return []
    
    def update_parametric_terms(self, items):
        """
        Set parametric terms in the parametric table
        """
        for child in self.Children:
            if isinstance(child,pdsim_panels.ParametricPanel):
                child.update_parametric_terms(items)
                
    def collect_output_terms(self):
        terms = []
        for panel in self.panels:
            if hasattr(panel,'collect_output_terms'):
                terms += panel.collect_output_terms()
        return terms
    
    def flush_parametric_terms(self):
        """ Remove all the entries in the parametric table """
        for panel in self.panels:
            if isinstance(panel, pdsim_panels.ParametricPanel):
                panel.flush_parametric_terms()
    
    def set_parametric_terms(self):
        """ Set the terms in the parameteric table """
        for panel in self.panels:
            if isinstance(panel, pdsim_panels.ParametricPanel):
                panel.set_parametric_terms()

class WriteOutputsPanel(wx.Panel):
    def __init__(self,parent):
        wx.Panel.__init__(self,parent)
        
        file_list = ['Temperature', 'Pressure', 'Volume', 'Density','Mass']
        #Create the box
        self.file_list = wx.CheckListBox(self, -1, choices = file_list)
        #Make them all checked
        self.file_list.SetCheckedStrings(file_list)
        
        btn = wx.Button(self,label='Select directory')
        btn.Bind(wx.EVT_BUTTON,self.OnWrite)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.file_list)
        sizer.Add(btn)
        self.SetSizer(sizer)
        
        self.Simulation = None
    
    def set_data(self,Simulation):
        """
        Set the internal simulation data for saving to file
        """
        self.Simulation=Simulation
        
    def OnWrite(self,event):
        """
        Event handler for selection of output folder for writing of files
        """
        dlg = wx.DirDialog(self, "Choose a directory:",
                          style=wx.DD_DEFAULT_STYLE|wx.DD_DIR_MUST_EXIST,
                           #| wx.DD_CHANGE_DIR
                           defaultPath=os.path.abspath(os.curdir)
                           )

        if dlg.ShowModal() == wx.ID_OK:
            self.WriteToFiles(dlg.GetPath())

        # Only destroy a dialog after you're done with it.
        dlg.Destroy()
    
    def WriteToFiles(self,dir_path):
        """
        Write the selected data to files in the folder given by dir_path
        """
        if self.Simulation is None:
            raise ValueError('Simulation data must be provied to WriteOutputsPanel')
        
        outputlist = self.file_list.GetCheckedStrings()
        #List of files that will be over-written
        OWList = [file+'.csv' for file in outputlist if os.path.exists(os.path.join(dir_path,file+'.csv'))]

        if OWList: #if there are any files that might get over-written
            
            dlg = wx.MessageDialog(None,message="The following files will be over-written:\n\n"+'\n'.join(OWList),caption="Confirm Overwrite",style=wx.OK|wx.CANCEL)
            if not dlg.ShowModal() == wx.ID_OK:
                #Don't do anything and return
                return
            
        for file in outputlist:
            if file == 'Pressure':
                xmat = self.Simulation.t
                ymat = self.Simulation.p
                pre = 'p'
            elif file == 'Temperature':
                xmat = self.Simulation.t
                ymat = self.Simulation.T
                pre = 'T'
            elif file == 'Volume':
                xmat = self.Simulation.t
                ymat = self.Simulation.V
                pre = 'V'
            elif file == 'Density':
                xmat = self.Simulation.t
                ymat = self.Simulation.rho
                pre = 'rho'
            elif file == 'Mass':
                xmat = self.Simulation.t
                ymat = self.Simulation.m
                pre = 'm'
            else:
                raise KeyError
            
            #Format for writing (first column is crank angle, following are data)
            joined = np.vstack([xmat,ymat]).T
            
            data_heads = [pre+'['+key+']' for key in self.Simulation.CVs.keys()]
            headers = 'theta [rad],'+ ','.join(data_heads)
            
            def row2string(array):
                return  ','.join([str(dummy) for dummy in array])
            
            rows = [row2string(joined[i,:]) for i in range(joined.shape[0])]
            s = '\n'.join(rows)
            
            #Actually write to file
            print 'writing data to ',os.path.join(dir_path,file+'.csv')
            fp = open(os.path.join(dir_path, file+'.csv'),'w')
            fp.write(headers+'\n')
            fp.write(s)
            fp.close()
            
        print 'You selected: %s\n' % dir_path

class RunToolBook(wx.Panel):
    def __init__(self,parent):
        wx.Panel.__init__(self, parent)
        
        # The running page of the main toolbook
        self.log_ctrl = wx.TextCtrl(self, wx.ID_ANY,
                                    style = wx.TE_MULTILINE|wx.TE_READONLY)
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.cmdAbort = wx.Button(self,-1,'Stop\nAll\nRuns')
        self.cmdAbort.Bind(wx.EVT_BUTTON, self.GetGrandParent().OnStop)
        hsizer = wx.BoxSizer(wx.HORIZONTAL)
        hsizer.Add(self.cmdAbort,0)
        sizer.Add(hsizer)
        sizer.Add(wx.StaticText(self,-1,"Output Log:"))
        sizer.Add(self.log_ctrl,1,wx.EXPAND)
        
        nb = wx.Notebook(self)
        self.log_ctrl_thread1 = wx.TextCtrl(nb, wx.ID_ANY,
                                            style = wx.TE_MULTILINE|wx.TE_READONLY)
        self.log_ctrl_thread2 = wx.TextCtrl(nb, wx.ID_ANY,
                                            style = wx.TE_MULTILINE|wx.TE_READONLY)
        self.log_ctrl_thread3 = wx.TextCtrl(nb, wx.ID_ANY,
                                            style = wx.TE_MULTILINE|wx.TE_READONLY)
        
        nb.AddPage(self.log_ctrl_thread1,"Thread #1")
        nb.AddPage(self.log_ctrl_thread2,"Thread #2")
        nb.AddPage(self.log_ctrl_thread3,"Thread #3")
        sizer.Add(nb,1,wx.EXPAND)
        self.write_log_button = wx.Button(self,-1,"Write Log to File")
        
        def WriteLog(event=None):
            FD = wx.FileDialog(None,"Log File Name",defaultDir=os.curdir,
                               style=wx.FD_SAVE|wx.FD_OVERWRITE_PROMPT)
            if wx.ID_OK==FD.ShowModal():
                fp=open(FD.GetPath(),'w')
                fp.write(self.log_ctrl.GetValue())
                fp.close()
            FD.Destroy()
            
        self.write_log_button.Bind(wx.EVT_BUTTON,WriteLog)
        sizer.Add(self.write_log_button,0)
        self.SetSizer(sizer)
        sizer.Layout()
        
class AutoWidthListCtrl(wx.ListCtrl, ListCtrlAutoWidthMixin):
    def __init__(self, parent, ID = wx.ID_ANY, pos=wx.DefaultPosition,
                 size=wx.DefaultSize, style=0):
        
        wx.ListCtrl.__init__(self, parent, ID, pos, size, style)
        ListCtrlAutoWidthMixin.__init__(self)
        
class ResultsList(wx.Panel, ColumnSorterMixin):
    def __init__(self, parent, headers, values, results):
        """
        
        parent : wx.Window
            parent of the Panel
            
        headers: a list of strings
            Each element is the string that will be the header of the column
            
        values: a list of list of values.  
            Each entry in the list should be as long as the number of headers
            
        results : PDSimCore instances
            The simulation runs
        """
        wx.Panel.__init__(self, parent)
        
        #: The list of strings of the header
        self.headers = list(headers)
        #: The values in the table
        self.values = list(values)
        #: The PDSimCore instances that have all the data
        self.results = list(results)
        
        self.list = AutoWidthListCtrl(self, 
                                      style=wx.LC_REPORT | wx.BORDER_NONE
                                      )
        #Build the headers
        for i, header in enumerate(headers):
            self.list.InsertColumn(i, header)
        
        #Add the values one row at a time
        self.itemDataMap = {}
        for i, row in enumerate(self.values):
            #Add an entry to the data map
            self.itemDataMap[i] = tuple(row)
            
            self.list.InsertStringItem(i,str(row[0]))
            self.list.SetItemData(i,i)
            
            for j in range(1,len(row)):
                self.list.SetStringItem(i,j,str(row[j]))
        
        total_width = 0    
        for i in range(len(headers)):
            self.list.SetColumnWidth(i, wx.LIST_AUTOSIZE_USEHEADER)
            total_width += self.list.GetColumnWidth(i)
            
        width_available = self.Parent.GetSize()[0]
        self.list.SetMinSize((width_available,200))
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.list,1,wx.EXPAND)
        
        self.il = wx.ImageList(16, 16)
        SmallUpArrow = PyEmbeddedImage(
            "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAABHNCSVQICAgIfAhkiAAAADxJ"
            "REFUOI1jZGRiZqAEMFGke2gY8P/f3/9kGwDTjM8QnAaga8JlCG3CAJdt2MQxDCAUaOjyjKMp"
            "cRAYAABS2CPsss3BWQAAAABJRU5ErkJggg==")
        SmallDnArrow = PyEmbeddedImage(
            "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAABHNCSVQICAgIfAhkiAAAAEhJ"
            "REFUOI1jZGRiZqAEMFGke9QABgYGBgYWdIH///7+J6SJkYmZEacLkCUJacZqAD5DsInTLhDR"
            "bcPlKrwugGnCFy6Mo3mBAQChDgRlP4RC7wAAAABJRU5ErkJggg==")
        
        self.sm_up = self.il.Add(SmallUpArrow.GetBitmap())
        self.sm_dn = self.il.Add(SmallDnArrow.GetBitmap())
        self.list.SetImageList(self.il, wx.IMAGE_LIST_SMALL)

        ColumnSorterMixin.__init__(self,len(headers)+1)
        
        self.SetSizer(sizer)
        self.SetAutoLayout(True)
        
    def OnSortOrderChanged(self, *args, **kwargs):
        """
        Overload the base class method to resort the internal structures 
        when the table is sorted
        """
        self._sort_objects()
        return ColumnSorterMixin.OnSortOrderChanged(self, *args, **kwargs)
        
    def __getitem__(self, index):
        """
        Provided to be able to index the class
        returns the index of the run returned
        """
        
        return self.results[index]
    
    def _sort_objects(self):
        """
        Sort the internal data structures based on the table sort state
        """
        
        # Sort the output csv table in the same way as the listctrl
        iCol, direction = self.GetSortState()
        
        #If sorted, sort the variables
        if iCol >= 0:
        
            # Get a sorted version of self.values sorted by the column used in list
            values_results = zip(self.values, self.results)
            
            # Sort the results and the rows together
            sorted_things = sorted(values_results, key=itemgetter(iCol))
            
            # Unpack
            self.values, self.results = zip(*sorted_things)
            
            # tuples --> list
            self.values = list(self.values)
            self.results = list(self.results)
        
    def remove_item(self, index):
        """
        Remove the item from the ResultsList instance
        """
        #Remove the item from the data map
        self.itemDataMap.pop(index)
        #Remove the item from the values
        del self.values[index]
        #Remove the item from the results
        del self.results[index]
        
    def get_results(self):
        """
        Return the list of PDSimCore instances
        """
        return self.results
 
    def GetListCtrl(self):
        """
        Required method for ColumnSorterMixin
        
        Used by the ColumnSorterMixin, see wx/lib/mixins/listctrl.py
        """
        return self.list
    
    def GetSortImages(self):
        """
        Required method for ColumnSorterMixin
        
        Used by the ColumnSorterMixin, see wx/lib/mixins/listctrl.py
        """
        return (self.sm_dn, self.sm_up)
    
    def AsString(self):
        """
        Return a csv formatted table of the ResultsList
        
        """
        
        #Sort the internal data structures based on table sort
        self._sort_objects()
        
        header_string = [','.join(self.headers)]
        def tostr(row):
            return [str(r) for r in row]
        rows_string = [','.join(tostr(row)) for row in self.values]
        return '\n'.join(header_string+rows_string)

class ColumnSelectionDialog(wx.Dialog):
    def __init__(self, parent, col_options, cols_selected):
        wx.Dialog.__init__(self,parent,size = (800,350))
        
        self.col_options = col_options

        self.selected = [col_options[col] for col in cols_selected]
        self.not_selected = [col_options[col] for col in col_options if col not in cols_selected]
        
        self.col_library_label = wx.StaticText(self, label = 'Available columns:')
        self.col_used_label = wx.StaticText(self, label = 'Selected columns:')
        self.col_library = wx.ListBox(self, choices = self.not_selected, style = wx.LB_EXTENDED)
        self.col_used = wx.ListBox(self, choices = self.selected, style = wx.LB_EXTENDED)
        self.col_library.SetMinSize((300,300))
        self.col_used.SetMinSize((300,300))
        
        #The central column with add and remove buttons
        self.AddAllButton=wx.Button(self, label='All ->')
        self.RemoveAllButton=wx.Button(self, label='<- All')
        self.AddButton=wx.Button(self, label='-->')
        self.RemoveButton=wx.Button(self, label='<--')
        self.AddButton.Bind(wx.EVT_BUTTON,self.OnAdd)
        self.RemoveButton.Bind(wx.EVT_BUTTON,self.OnRemove)
        self.AddAllButton.Bind(wx.EVT_BUTTON,self.OnAddAll)
        self.RemoveAllButton.Bind(wx.EVT_BUTTON,self.OnRemoveAll)
        vsizer = wx.BoxSizer(wx.VERTICAL)
        vsizer.AddMany([self.AddAllButton, self.RemoveAllButton])
        vsizer.AddSpacer(40)
        vsizer.AddMany([self.AddButton, self.RemoveButton])

        #The far-right column with up,down, ok, cancel buttons      
        self.Up = wx.Button(self, label='Move Up')
        self.Up.Bind(wx.EVT_BUTTON,self.OnUp)
        self.Down = wx.Button(self, label='Move Down')
        self.Down.Bind(wx.EVT_BUTTON,self.OnDown)
        self.OkButton = wx.Button(self, label='Ok')
        self.OkButton.Bind(wx.EVT_BUTTON,self.OnAccept)
        self.CancelButton = wx.Button(self, label='Cancel')
        self.CancelButton.Bind(wx.EVT_BUTTON,self.OnClose)
        vsizer2 = wx.BoxSizer(wx.VERTICAL)
        vsizer2.AddMany([self.Up,self.Down])
        vsizer2.AddSpacer(40)
        vsizer2.AddMany([self.CancelButton, self.OkButton])
        
        #Layout the dialog
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        vsizer0 = wx.BoxSizer(wx.VERTICAL)
        vsizer0.Add(self.col_library_label)
        vsizer0.Add(self.col_library, 1, wx.EXPAND)
        sizer.Add(vsizer0)
        sizer.AddSpacer(10)
        sizer.Add(vsizer,0,wx.ALIGN_CENTER_VERTICAL)
        sizer.AddSpacer(10)
        vsizer20 = wx.BoxSizer(wx.VERTICAL)
        vsizer20.Add(self.col_used_label)
        vsizer20.Add(self.col_used, 1, wx.EXPAND)
        sizer.Add(vsizer20)
        sizer.AddSpacer(10)
        sizer.Add(vsizer2,0,wx.ALIGN_CENTER_VERTICAL)
        self.SetSizer(sizer)
        sizer.Layout()
        
        #Bind a key-press event to all objects to get Esc key press
        children = self.GetChildren()
        for child in children:
            child.Bind(wx.EVT_KEY_UP,  self.OnKeyPress) 

    def OnKeyPress(self,event):
        """ cancel if Escape key is pressed """
        event.Skip()
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
        elif event.GetKeyCode() == wx.WXK_RETURN:
            self.EndModal(wx.ID_OK)
        
    def label2attr(self,label):
        for col in self.col_options:
            if self.col_options[col] == label:
                return col
        raise KeyError
        
    def OnAccept(self, event):
        self.EndModal(wx.ID_OK)
        
    def OnClose(self,event):
        self.EndModal(wx.ID_CANCEL)
        
    def OnAddAll(self, event):
        self.selected += self.not_selected
        self.not_selected = []
        self.col_library.SetItems(self.not_selected)
        self.col_used.SetItems(self.selected)
        
    def OnRemoveAll(self, event):
        self.not_selected += self.selected
        self.selected = []
        self.col_library.SetItems(self.not_selected)
        self.col_used.SetItems(self.selected)
        
    def OnAdd(self, event):
        indices = self.col_library.GetSelections()
        labels = [self.col_library.GetString(index) for index in indices]

        for label in reversed(labels):
            i = self.not_selected.index(label)
            self.selected.append(self.not_selected.pop(i))
        self.col_library.SetItems(self.not_selected)
        self.col_used.SetItems(self.selected)
        
    def OnRemove(self, event):
        indices = self.col_used.GetSelections()
        labels = [self.col_used.GetString(index) for index in indices]

        for label in reversed(labels):
            i = self.selected.index(label)
            self.not_selected.append(self.selected.pop(i))
        self.col_library.SetItems(self.not_selected)
        self.col_used.SetItems(self.selected)
        
    def OnUp(self, event):
        indices = self.col_used.GetSelections()
        labels = [self.col_used.GetString(index) for index in indices]
        for label in labels:
            i = self.selected.index(label)
            if i>0:
                #swap item and the previous item
                self.selected[i-1],self.selected[i]=self.selected[i],self.selected[i-1]
        self.col_used.SetItems(self.selected)
        if len(labels) == 1:
            self.col_used.SetSelection(indices[0]-1)
    
    def OnDown(self, event):
        indices = self.col_used.GetSelections()
        labels = [self.col_used.GetString(index) for index in indices]
        for label in labels:
            i = self.selected.index(label)
            if i<len(self.selected)-1:
                #swap item and the next item
                self.selected[i+1],self.selected[i]=self.selected[i],self.selected[i+1]
        self.col_used.SetItems(self.selected)
        if len(labels) == 1:
            self.col_used.SetSelection(indices[0]+1)
    
    def GetSelections(self):
        labels = self.col_used.GetStrings()
        attrs = [self.label2attr(label) for label in labels]
        return attrs

class FileOutputDialog(wx.Dialog):
    def __init__(self,Simulations, table_string):
        wx.Dialog.__init__(self,None)
        self.Simulations = Simulations
        self.table_string = table_string
        
        #The root directory selector
        hsizer = wx.BoxSizer(wx.HORIZONTAL)
        hsizer.Add(wx.StaticText(self,label="Output Directory:"))
        self.txtDir = wx.TextCtrl(self,value ='.')
        self.txtDir.SetMinSize((200,-1))
        hsizer.Add(self.txtDir,1,wx.EXPAND)
        self.cmdDirSelect = wx.Button(self,label="Select...")
        self.cmdDirSelect.Bind(wx.EVT_BUTTON,self.OnDirSelect)
        hsizer.Add(self.cmdDirSelect)
        
        
        #The CSV selections
        file_list = ['Temperature', 'Pressure', 'Volume', 'Density','Mass']
        #Create the box
        self.file_list = wx.CheckListBox(self, choices = file_list)
        #Make them all checked
        self.file_list.SetCheckedStrings(file_list)
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(hsizer)
        sizer.AddSpacer(10)
        sizer.Add(wx.StaticText(self,label='CSV files:'))
        sizer.Add(self.file_list)
        
        sizer.AddSpacer(10)
        self.chkPickled = wx.CheckBox(self,label='HDF5 data files (Warning! Can be quite large)')
        self.chkPickled.SetToolTipString('Hint: You can use ViTables (search Google) to open the HDF5 files')
        self.chkPickled.SetValue(True)
        sizer.Add(self.chkPickled)
        
        sizer.AddSpacer(10)
        self.chkTable = wx.CheckBox(self,label='Tabular data')
        self.chkTable.SetValue(True)
        sizer.Add(self.chkTable)
        
        self.cmdWrite = wx.Button(self, label = 'Write!')
        self.cmdWrite.Bind(wx.EVT_BUTTON, self.OnWrite)
        sizer.AddSpacer(10)
        sizer.Add(self.cmdWrite)
        self.SetSizer(sizer)
        
    def OnDirSelect(self,event):
        #
        os.chdir(os.curdir)
        dlg = wx.DirDialog(None, "Choose a directory:",
                           defaultPath = os.path.abspath(os.curdir),
                           style=wx.DD_DEFAULT_STYLE | wx.DD_NEW_DIR_BUTTON
                           )
        if dlg.ShowModal() == wx.ID_OK:
            self.txtDir.SetValue(dlg.GetPath())
        dlg.Destroy()
        
    def OnWrite(self, event):
        """
        
        """
        dir_path = self.txtDir.GetValue()
        if not os.path.exists(dir_path):
            dlg = wx.MessageDialog(None, message = 'Selected output directory does not exist.  Please select a folder then try again')
            dlg.ShowModal()
            dlg.Destroy()
            return
        
        for i, sim in enumerate(self.Simulations):
            if (self.file_list.GetCheckedStrings() or 
                self.chkPickled.GetValue()):
                
                run_path = 'RunNumber{0:04d}'.format(i+1)
                if not os.path.exists(os.path.join(dir_path, run_path)):
                    os.mkdir(os.path.join(dir_path, run_path))
                self.write_csv_files(os.path.join(dir_path, run_path), sim)
            
            if self.chkPickled.GetValue():
                self.write_pickle(os.path.join(dir_path, run_path), sim)
            if self.chkTable.GetValue():
                fp = open(os.path.join(dir_path,'ResultsTable.csv'),'w')
                fp.write(self.table_string)
                fp.close()
        self.Destroy()
    
    def write_pickle(self, dir_path, sim):
        from plugins.HDF5_plugin import HDF5Writer
        hdf5_path = os.path.join(dir_path,'Simulation.h5')
        HDF5 = HDF5Writer()
        HDF5.write_to_file(sim, hdf5_path)
        
    def write_csv_files(self, dir_path, sim):
        """
        Write the selected data to files in the folder given by dir_path
        """
        
        outputlist = self.file_list.GetCheckedStrings()
            
        #List of files that will be over-written
        OWList = [file+'.csv' for file in outputlist if os.path.exists(os.path.join(dir_path, file+'.csv'))]

        if OWList: #if there are any files that might get over-written
            
            dlg = wx.MessageDialog(None, message="The following files will be over-written:\n\n"+'\n'.join(OWList),caption="Confirm Overwrite",style=wx.OK|wx.CANCEL)
            if not dlg.ShowModal() == wx.ID_OK:
                #Don't do anything and return
                return wx.ID_CANCEL

        for file in outputlist:
            if file == 'Pressure':
                xmat = sim.t
                ymat = sim.p
                pre = 'p'
            elif file == 'Temperature':
                xmat = sim.t
                ymat = sim.T
                pre = 'T'
            elif file == 'Volume':
                xmat = sim.t
                ymat = sim.V
                pre = 'V'
            elif file == 'Density':
                xmat = sim.t
                ymat = sim.rho
                pre = 'rho'
            elif file == 'Mass':
                xmat = sim.t
                ymat = sim.m
                pre = 'm'
            else:
                raise KeyError
            
            #Format for writing (first column is crank angle, following are data)
            joined = np.vstack([xmat,ymat]).T
            
            data_heads = [pre+'['+key+']' for key in sim.CVs.keys()]
            headers = 'theta [rad],'+ ','.join(data_heads)
            
            def row2string(array):
                return  ','.join([str(dummy) for dummy in array])
            
            rows = [row2string(joined[i,:]) for i in range(joined.shape[0])]
            s = '\n'.join(rows)
            
            #Actually write to file
            print 'writing data to ',os.path.join(dir_path,file+'.csv')
            fp = open(os.path.join(dir_path,file+'.csv'),'w')
            fp.write(headers+'\n')
            fp.write(s)
            fp.close()
    
class OutputDataPanel(pdsim_panels.PDPanel):
    def __init__(self, parent, variables, configfile, **kwargs):
        pdsim_panels.PDPanel.__init__(self, parent, **kwargs)
        
        self.results = []
        
        self.variables = variables
        #Make the items
        cmdLoad = wx.Button(self, label = "Add Runs...")
        cmdRefresh = wx.Button(self, label = "Refresh")
        cmdSelect = wx.Button(self, label = "Select Columns...")
        #Make the sizers
        hsizer = wx.BoxSizer(wx.HORIZONTAL)
        sizer = wx.BoxSizer(wx.VERTICAL)
        #Put the things into sizers
        hsizer.Add(cmdLoad)
        hsizer.Add(cmdSelect)
        hsizer.Add(cmdRefresh)
        sizer.Add(hsizer)
        #Bind events
        cmdLoad.Bind(wx.EVT_BUTTON, self.OnLoadRuns)
        cmdSelect.Bind(wx.EVT_BUTTON, self.OnSelectCols)
        cmdRefresh.Bind(wx.EVT_BUTTON, self.OnRefresh)
        
        self.SetSizer(sizer)
        self.ResultsList = None
        self.WriteButton = None
        
        #Create a list of possible columns
        self.column_options = {'mdot': 'Mass flow rate [kg/s]',
                               'eta_v': 'Volumetric efficiency [-]',
                               'eta_a': 'Adiabatic efficiency [-]',
                               'Td': 'Discharge temperature [K]',
                               'motor.losses': 'Motor losses [kW]',
                               'Wdot_electrical': 'Electrical power [kW]',
                               'Wdot_mechanical': 'Mechanical power [kW]',
                               'Qamb': 'Ambient heat transfer [kW]',
                               'run_index': 'Run Index',
                               'eta_oi': 'Overall isentropic efficiency [-]',
                               'elapsed_time': 'Elapsed time [s]'
                               }
        
        # Add all the parameters that arrive from the self.items lists from the 
        # pdsim_panels.PDPanel instances from the SolverToolbook and the 
        # InputsToolbook
        for var in self.variables:
            key = var['attr']
            value = var['text']
            self.column_options[key] = value
        
        self.columns_selected = []
        sizer.Layout()
        
        self.Bind(wx.EVT_SIZE, self.OnRefresh)
        
        self.get_from_configfile('OutputDataPanel')
    
    def _get_nested_attr(self,sim,attr):
        """
        Get a nested attribute of the class.  Can be a combination of indexing
        as well as dotted attribute access.  For instance
        
            "injection.massflow['injection_line.1.1']"
            
        The only delimiters allowed are [ ] . ' 
        
        Returns
        -------
        The value if found, ``None`` otherwise
        """   
        def _clean_string(_string):
            """ returns True if doesn't contain any of the delimiters """
            return (_string.find(".") < 0
                    and _string.find("[") < 0
                    and _string.find("]") < 0
                    and _string.find("'") < 0)
        
        def _next_delimiter(_string):
            """ returns the next delimiter, or None if none found """
            dels = ["[","'","."]
            i = 9999
            for _del in dels:
                Ifound = _string.find(_del)
                if Ifound >= 0 and Ifound < i:
                    i = Ifound
            
            if i == 9999:
                return None
            else:
                return _string[i] 
        
        def _last_delimiter(_string):
            """ returns the last delimiter, or None if none found """
            dels = ["[","'","."]
            i = -1
            for _del in dels:
                Ifound = _string.find(_del)
                if Ifound >= 0 and Ifound > i:
                    i = Ifound
            
            if i == -1:
                return None
            else:
                return _string[i]
                
        var = sim
        while len(attr)>0:
            
            # Just return the attribute if it has it
            if hasattr(var, attr):
                return getattr(var, attr)
            
            # If the attr you are looking up has no funny delimiters, but isn't
            # in the class, return None (not found)
            elif _clean_string(attr):
                return None
            
            # Split at the dot (.) if there is nothing funny in the left part
            elif len(attr.split('.')) > 1 and _clean_string(attr.split('.',1)[0]):
                # Actually split the string
                left, right = attr.split('.',1)
                # Update variable using the left side, update attribute using right
                if hasattr(var, left):
                    var = getattr(var, left)
                    attr = right
                else:
                    return None
            
            # If the next delimiter is a [ but does not start the string, 
            # you are doing some sort of indexing.  This can't be a return
            # value
            elif _next_delimiter(attr) == '[' and not attr[0] == '[':
                # Actually split the string
                left, right = attr.split('[',1)
                #Right gets its bracket back
                right = '[' + right
                # Update variable using the left side, update attribute using right
                if hasattr(var, left):
                    var = getattr(var,left)
                    attr = right
                else:
                    return None
            
            #If the next thing is an index
            elif attr[0] == '[':
                #Remove the leading bracket
                attr = attr[1:len(attr)]
                #Get the part in-between the brackets, rest is attr
                index_string, attr = attr.split(']', 1)
                #If it is wrapped in single-quotes it is a string, otherwise it must be an integer
                try:
                    if index_string[0] == "'" and index_string[-1] == "'":
                        # It is a string, discard the single-quotes
                        index_string = index_string[1:len(index_string)-1]
                        # A string index
                        var = var[index_string]
                    else:
                        # An integer index
                        var = var[int(index_string)]
                        
                except IndexError:
                    return None
                
                if len(attr)==0:
                    return var             
                    
    def _hasattr(self, sim, attr):
        #If the attribute exists at the top-level of the simulation class
        if hasattr(sim, attr):
            return True
        else:
            val = self._get_nested_attr(sim, attr)
            if val is None:
                return False
            else:
                return True 
        
    def rebuild(self):
        
        if self.results: #as long as it isn't empty
            
            #Remove the items on the panel
            if self.ResultsList is not None:
                self.WriteButton.Destroy()
                self.RemoveButton.Destroy()
                self.PlotButton.Destroy()
                self.ResultsList.Destroy()
                self.GetSizer().Layout()
            
            #Remove any keys that are in cols_selected but not in col_options
            #Iterate over a copy so that removing the keys works properly
            for key in self.columns_selected[:]:
                if key not in self.column_options:
                    self.columns_selected.remove(key)
                    warnings.warn('The key '+key+' was found in columns_selected but not in column_options.')
                
            for attr in reversed(self.columns_selected):
                #If the key is in any of the simulations 
                if not any([self._hasattr(sim,attr) for sim in self.results]):
                    print 'removing column_heading', attr,' since it is not found in any simulation'
                    self.columns_selected.remove(attr)
                        
            rows = []
            for sim in self.results: #loop over the results
                row = []
                for attr in self.columns_selected:
                    if self._hasattr(sim, attr):
                        value = self._get_nested_attr(sim, attr)
                        row.append(value)
                    else:
                        print 'Trying to add attribute \'' + attr + '\' to output but it is not found found in simulation instance'
                rows.append(row)
            headers = [self.column_options[attr] for attr in self.columns_selected]
            
            #The items being created
            self.ResultsList = ResultsList(self, headers, rows, self.results)

            
            self.WriteButton = wx.Button(self, label = 'Write to file...')
            self.WriteButton.Bind(wx.EVT_BUTTON, self.OnWriteFiles)
            
            self.RemoveButton = wx.Button(self,label = 'Remove selected')
            self.RemoveButton.Bind(wx.EVT_BUTTON, self.OnRemoveSelected)
            
            self.PlotButton = wx.Button(self,label = 'Plot selected')
            self.PlotButton.Bind(wx.EVT_BUTTON, self.OnPlotSelected)
            
            #Do the layout of the panel
            sizer = self.GetSizer()
            
            hsizer = wx.BoxSizer(wx.HORIZONTAL)
            hsizer.Add(self.ResultsList,1,wx.EXPAND)
            sizer.Add(hsizer)
            
            hsizer = wx.BoxSizer(wx.HORIZONTAL)
            hsizer.Add(self.WriteButton)
            hsizer.Add(self.RemoveButton)
            hsizer.Add(self.PlotButton)
            sizer.Add(hsizer)
            
            sizer.Layout()
            self.Refresh()
            
        else:
            #Destroy the items associated with the output data
            if self.ResultsList is not None:
                self.WriteButton.Destroy()
                self.RemoveButton.Destroy()
                self.PlotButton.Destroy()
                self.ResultsList.Destroy()
                self.ResultsList = None
                
            sizer = self.GetSizer()
            sizer.Layout()
            self.Refresh()
    
    def add_runs(self, results, rebuild = False):
        self.results += results
        if rebuild:
            self.rebuild()
            
    def add_output_terms(self, items):
        
        for var in items:
            key = var['attr']
            value = var['text']
            self.column_options[key] = value
      
    def change_output_attrs(self, key_dict):
        """
        Change column attributes
        
        For instance::
        
            change_output_attrs( dict(t = 'geo.t') )
        
        Parameters
        ----------
        key_dict : dict
            A dictionary with keys of old key and value of new key
        """
        for old_key,new_key in key_dict.iteritems():
            #Replace the value in columns selected if it is selected
            if old_key in self.columns_selected:
                i = self.columns_selected.index(old_key)
                self.columns_selected[i] = new_key
            if old_key in self.column_options:
                #Make a copy using the old_key
                val = self.column_options.pop(str(old_key))
                #Use the old value with the updated key
                self.column_options[new_key] = val
            
        self.rebuild()
        
    def OnLoadRuns(self, event = None):
        """
        Load a pickled run from a file
        """
        home = os.getenv('USERPROFILE') or os.getenv('HOME')
        temp_folder = os.path.join(home,'.pdsim-temp')
        
        FD = wx.FileDialog(None,"Load Runs",defaultDir = temp_folder,
                           wildcard = 'PDSim Runs (*.mdl)|*.mdl',
                           style=wx.FD_OPEN|wx.FD_MULTIPLE|wx.FD_FILE_MUST_EXIST)
        if wx.ID_OK == FD.ShowModal():
            file_paths = FD.GetPaths()
            for file in file_paths:
                sim = cPickle.load(open(file,'rb'))
                self.add_runs([sim])
            self.rebuild()
        FD.Destroy()
    
    def OnPlotSelected(self, event):
        list_ = self.ResultsList.GetListCtrl()
        
        indices = []
        index = list_.GetFirstSelected()
        sim = self.results[index]
        self.Parent.plot_outputs(sim)
        
        if list_.GetNextSelected(index) != -1:
            dlg = wx.MessageDialog(None,'Sorry, only the first selected row will be used')
            dlg.ShowModal()
            dlg.Destroy()
                
    def OnRemoveSelected(self, event):
        list_ = self.ResultsList.GetListCtrl()
        
        indices = []
        index = list_.GetFirstSelected()
        while index != -1:
            indices.append(index)
            index = list_.GetNextSelected(index)
            
        #Some runs to delete
        if indices:
            #Warn before removing
            dlg = wx.MessageDialog(None,'You are about to remove '+str(len(indices))+' runs.  Ok to confirm', style = wx.OK|wx.CANCEL)
            if dlg.ShowModal() == wx.ID_OK:
                for index in reversed(indices):
                    #Remove the item from the ResultsList
                    self.ResultsList.remove_item(index)
                    
                #Update our copy of the results
                self.results = self.ResultsList.get_results()
                
            dlg.Destroy()
            
            #Rebuild the ResultsList
            self.rebuild()
                
    
    def OnWriteFiles(self, event):
        """
        Event that fires when the button is clicked to write a selection of things to files
        """
        table_string = self.ResultsList.AsString()
        dlg = FileOutputDialog(self.results, table_string = table_string)
        dlg.ShowModal()
        dlg.Destroy()
        
    def OnRefresh(self, event):
        self.rebuild()
        
    def OnSelectCols(self, event = None):
        dlg = ColumnSelectionDialog(None, self.column_options, self.columns_selected)
        if dlg.ShowModal() == wx.ID_OK:
            self.columns_selected = dlg.GetSelections() 
        dlg.Destroy()
        self.rebuild()
    
    def post_get_from_configfile(self, key, value):
        if not key == 'selected':
            raise KeyError
        
        list_str = value.split(',',1)[1].replace("'","").replace('[','').replace(']','').replace("u'","'")
        for attr in list_str.split(';'):
            #Strip leading and trailing space
            attr = attr.strip()
            self.columns_selected.append(attr)
    
    def post_prep_for_configfile(self):
        return 'selected  = selected,'+str(self.columns_selected).replace(',',';').replace("u'","'")+'\n'
        
class OutputsToolBook(wx.Toolbook):
    def __init__(self,parent,configfile):
        wx.Toolbook.__init__(self, parent, wx.ID_ANY, style=wx.BK_LEFT)
        il = wx.ImageList(32, 32)
        indices=[]
        for imgfile in ['Geometry.png','MassFlow.png']:
            ico_path = os.path.join('ico',imgfile)
            indices.append(il.Add(wx.Image(ico_path,wx.BITMAP_TYPE_PNG).ConvertToBitmap()))
        self.AssignImageList(il)
        
        variables = self.Parent.InputsTB.collect_parametric_terms()
        self.PlotsPanel = wx.Panel(self)
        self.DataPanel = OutputDataPanel(self,
                                         variables = variables, 
                                         name = 'OutputDataPanel',
                                         configfile = configfile)
        
        #Make a Recip instance
        self.panels=(self.DataPanel,self.PlotsPanel)
        for Name,index,panel in zip(['Data','Plots'],indices,self.panels):
            self.AddPage(panel,Name,imageId=index)
            
        self.PN = None
            
    def plot_outputs(self, recip = None):
        parent = self.PlotsPanel
        # First call there is no plot notebook in existence
        if self.PN is None:
            self.PN = PlotNotebook(recip,parent)
            sizer = wx.BoxSizer(wx.VERTICAL)
            sizer.Add(self.PN,1,wx.EXPAND)
            parent.SetSizer(sizer)
            parent.Fit() ##THIS IS VERY IMPORTANT!!!!!!!!!!! :)
        else:
            self.PN.update(recip)
            
    def add_output_terms(self, items):
        self.DataPanel.add_output_terms(items)
        
    def change_output_terms(self, key_dict):
        self.DataPanel.change_output_terms(key_dict)
            
class MainToolBook(wx.Toolbook):
    def __init__(self,parent,configfile):
        wx.Toolbook.__init__(self, parent, -1, style=wx.BK_TOP)
        il = wx.ImageList(32, 32)
        indices=[]
        for imgfile in ['Inputs.png','Solver.png','Solver.png','Outputs.png']:
            ico_path = os.path.join('ico',imgfile)
            indices.append(il.Add(wx.Image(ico_path,wx.BITMAP_TYPE_PNG).ConvertToBitmap()))
        self.AssignImageList(il)
        
        self.InputsTB = InputsToolBook(self, configfile)
        self.SolverTB = SolverToolBook(self, configfile)
        self.RunTB = RunToolBook(self)
        self.OutputsTB = OutputsToolBook(self, configfile)
        
        self.panels=(self.InputsTB,self.SolverTB,self.RunTB,self.OutputsTB)
        for Name,index,panel in zip(['Inputs','Solver','Run','Output'],indices,self.panels):
            self.AddPage(panel,Name,imageId=index)

class MainFrame(wx.Frame):
    def __init__(self, configfile=None, position=None, size=None):
        wx.Frame.__init__(self, None, title = "PDSim GUI", size=(700, 700))
        
        if configfile is None: #No file name or object passed in
            
            configfile = os.path.join('configs','default.cfg')
            
            #First see if a command line option provided
            if '--config' in sys.argv:
                i = sys.argv.index('--config')
                _configfile = sys.argv[i+1]
                if os.path.exists(_configfile):
                    configbuffer = open(_configfile, 'rb')
                else:
                    warnings.warn('Sorry but your --config file "'+_configfile+'" is not found')
                    configbuffer = open(configfile, 'rb')
                
            #Then see if there is a file at configs/default.cfg
            elif os.path.exists(configfile):
                configbuffer = open(configfile,'rb')
                
            #Then use the internal default recip
            else:
                configbuffer = default_configs.get_recip_defaults()
        
        #A string has been passed in for the 
        elif isinstance(configfile, basestring):
            if os.path.exists(configfile):
                configbuffer = open(configfile, 'rb')
                        
        elif isinstance(configfile, StringIO.StringIO):
            configbuffer = configfile
                
        else:
            raise ValueError
        
        #Get a unicode-wrapped version of the selected file-like object
        enc, dec, reader, writer = codecs.lookup('latin-1')
        uConfigParser = reader(configbuffer)
                
        #The file-like object for the configuration
        self.config_parser = SafeConfigParser()
        self.config_parser.optionxform = unicode 
        self.config_parser.readfp(uConfigParser)
        
        #The file-like object for the default scroll configuration
        parser = SafeConfigParser()
        parser.optionxform = unicode
        uConfigParser = reader(default_configs.get_scroll_defaults())
        parser.readfp(uConfigParser)
        self.config_parser_default_scroll = parser
        
        #The file-like object for the default recip configuration
        parser = SafeConfigParser()
        parser.optionxform = unicode
        uConfigParser = reader(default_configs.get_recip_defaults())
        parser.readfp(uConfigParser)
        self.config_parser_default_recip = parser
        
        #Get the simulation type (recip, scroll, ...)
        self.SimType = self.config_parser.get('Globals', 'Type')
            
        #The position and size are needed when the frame is rebuilt, but not otherwise
        if position is None:
            position = (-1,-1)
        if size is None:
            size = (-1,-1)
        
        #Use the builder function to rebuild using the configuration objects
        self.build()
        
        # Set up redirection of input and output to logging wx.TextCtrl
        # Taken literally from http://www.blog.pythonlibrary.org/2009/01/01/wxpython-redirecting-stdout-stderr/
        class RedirectText(object):
            def __init__(self,aWxTextCtrl):
                self.out=aWxTextCtrl
            def write(self, string):
                wx.CallAfter(self.out.AppendText, string)
#            def flush(self):
#                return None
                
        redir=RedirectText(self.MTB.RunTB.log_ctrl)
        sys.stdout=redir
        sys.stderr=redir
        
        self.SetPosition(position)
        self.SetSize(size)
        
        self.worker = None
        self.workers = None
        self.WTM = None
        
        #: A thread-safe queue for the processing of the results 
        self.results_list = Queue()
        
        # Bind the idle event handler that will always run and
        # deal with the results
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self.OnIdle, self.timer)
        self.timer.Start(1000) #1000 ms between checking the queue
        
        self.default_configfile = default_configs.get_recip_defaults()
        
    def get_config_objects(self):
        if self.SimType == 'recip':
            return (self.config_parser, self.config_parser_default_recip)
        elif self.SimType == 'scroll':
            return (self.config_parser, self.config_parser_default_scroll)
        else:
            raise AttributeError
    
    def get_logctrls(self):
        return [self.MTB.RunTB.log_ctrl_thread1,
                self.MTB.RunTB.log_ctrl_thread2,
                self.MTB.RunTB.log_ctrl_thread3]
    
    def update_parametric_terms(self):
        """
        Actually update the parametric terms in the parametric table options
        """
        para_terms = self.collect_parametric_terms()
        self.MTB.SolverTB.update_parametric_terms(para_terms)
        
    def collect_parametric_terms(self):
        """
        This function is called to find all the parametric terms that are
        required.
        
        They can be recursively found in:
        - self.items in PDPanel instances
        - collect_parametric_terms in PDPanel instances
        - 
        
        """
        terms = []
        #Loop over the toolbooks and allow them to collect their own terms
        for child in self.MTB.Children:
            if isinstance(child,wx.Toolbook) and hasattr(child,'collect_parametric_terms'):
                terms += child.collect_parametric_terms()
        return terms
        
    def rebuild(self, configfile):
        """
        Destroy everything in the main frame and recreate 
        the contents based on parsing the config file
        """
        # Create a new instance of the MainFrame class using the 
        # new configuration file name and the current location of
        # the frame
        position = self.GetPosition()
        size = self.GetSize()
        frame = MainFrame(configfile, position=position, size=size)
        frame.Show()
        
        #Destroy the current MainFrame
        self.Destroy()
        
    def build(self):
        self.make_menu_bar()
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        self.MTB=MainToolBook(self, self.get_config_objects())
        sizer.Add(self.MTB, 1, wx.EXPAND)
        self.SetSizer(sizer)
        sizer.Layout()
        
        self.worker=None
        
        self.load_plugins(self.PluginsMenu)
        #After loading plugins, try to set the parameters in the parametric table
        self.MTB.SolverTB.flush_parametric_terms()
        self.MTB.SolverTB.set_parametric_terms()
            
    def build_recip(self, post_set = True):
        #Instantiate the recip class
        recip=Recip()
        #Pull things from the GUI as much as possible
        self.MTB.InputsTB.set_params(recip)
        self.MTB.SolverTB.set_params(recip)
        if post_set:
            self.MTB.InputsTB.post_set_params(recip)
            self.MTB.SolverTB.post_set_params(recip)
            #Build the model the rest of the way
            RecipBuilder(recip)
        return recip
    
    def build_scroll(self, post_set = True, apply_plugins = True):
        """
        Build the scroll simulation
        
        Parameters
        ----------
        post_set : boolean, optional
            If ``True``, post_set_params will be called on PDPanel instances 
            and ScrollBuilder will be called
            
        apply_plugins: boolean, optional
            If ``True``, all the plugins will be applied when this function is called
        """
        #Instantiate the scroll class
        scroll=Scroll()
        #Pull things from the GUI as much as possible
        self.MTB.InputsTB.set_params(scroll)
        self.MTB.SolverTB.set_params(scroll)
        if post_set:
            self.MTB.InputsTB.post_set_params(scroll)
            self.MTB.SolverTB.post_set_params(scroll)
            #Build the model the rest of the way
            ScrollBuilder(scroll)
        if apply_plugins:
            #Apply any plugins in use - this is the last step
            if hasattr(self,'plugins_list') and self.plugins_list:
                for plugin in self.plugins_list:
                    if plugin.is_activated():
                        plugin.apply(scroll)
        return scroll
    
    def run_simulation(self, sim):
        """
        Run a single simulation
        """
            
        #Make single-run into a list in order to use the code for the batch
        self.run_batch([sim])
    
    def run_batch(self, sims):
        """
        Run a list of simulations
        """
        if self.WTM is None:
            self.MTB.SetSelection(2)
            self.WTM = WorkerThreadManager(Run1, sims, self.get_logctrls(),args = tuple(),
                                           done_callback = self.deliver_result,
                                           main_stdout = self.MTB.RunTB.log_ctrl)
            self.WTM.setDaemon(True)
            self.WTM.start()
        else:
            dlg = wx.MessageDialog(None,"Batch has already started.  Wait until completion or kill the batch","")
            dlg.ShowModal()
            dlg.Destroy()
            
    def deliver_result(self, sim = None):
        if sim is not None:
#            print 'Queueing a result for further processing'
            self.results_list.put(sim)
            wx.CallAfter(self.MTB.RunTB.log_ctrl.WriteText,'Result queued\n') 
     
    def load_plugins(self, PluginsMenu):
        """
        Load any plugins into the GUI that are found in plugins folder
        
        It is recommended that all classes and GUI elements relevant to the 
        plugin be included in the given python file
        """
        import glob
        self.plugins_list = []
        #Look at each .py file in plugins folder
        for py_file in glob.glob(os.path.join('plugins','*.py')):
            #Get the root filename (/path/to/AAA.py --> AAA)
            fname = py_file.split(os.path.sep,1)[1].split('.')[0]
            
            mods = __import__('plugins.'+fname)
            #Try to import the file as a module
            mod = getattr(mods,fname)
            for term in dir(mod):
                thing = getattr(mod,term)
                try:
                    #If it is a plugin class
                    if issubclass(thing, pdsim_plugins.PDSimPlugin):
                        
                        #Instantiate the plugin
                        plugin = thing()
                        
                        #Give the plugin a link to the main wx.Frame
                        plugin.set_GUI(self)
                        
                        #Check if it should be enabled, if not, go to the next plugin
                        if not plugin.should_enable():
                            del plugin
                            continue
                                                
                        #Append an instance of the plugin to the list of plugins
                        self.plugins_list.append(plugin)
                        
                        #Create a menu item for the plugin
                        menuItem = wx.MenuItem(self.Type, -1, thing.short_description, "", wx.ITEM_CHECK)
                        PluginsMenu.AppendItem(menuItem)
                        #Bind the event to activate the plugin
                        self.Bind(wx.EVT_MENU, plugin.activate, menuItem)
                                                
                        # Check if this type of plugin is included in the config
                        # file
                        for section in self.config_parser.sections():
                            if (section.startswith('Plugin')
                                    and section.split(':')[1] ==  term):
                                # If it is, activate it and check the element
                                # in the menu
                                plugin.activate()
                                menuItem.Check(True)
                                
                                # Pass the section along to the plugin
                                items = self.config_parser.items(section)
                                plugin.build_from_configfile_items(items)
                        
                except TypeError:
                    pass
        
        # Update the parametric terms in the parametric tables because the 
        # plugins might have added terms if they are activated from the config
        # file
        self.update_parametric_terms()
        
    def make_menu_bar(self):
        #################################
        ####       Menu Bar         #####
        #################################
        
        # Menu Bar
        self.MenuBar = wx.MenuBar()
        
        self.File = wx.Menu()
        self.menuFileOpen = wx.MenuItem(self.File, -1, "Open Config from file...\tCtrl+O", "", wx.ITEM_NORMAL)
        self.menuFileSave = wx.MenuItem(self.File, -1, "Save config to file...\tCtrl+S", "", wx.ITEM_NORMAL)
        self.menuFileFlush = wx.MenuItem(self.File, -1, "Flush out temporary files...", "", wx.ITEM_NORMAL)
        self.menuFileConsole = wx.MenuItem(self.File, -1, "Open a python console", "", wx.ITEM_NORMAL)
        self.menuFileQuit = wx.MenuItem(self.File, -1, "Quit\tCtrl+Q", "", wx.ITEM_NORMAL)
        
        self.File.AppendItem(self.menuFileOpen)
        self.File.AppendItem(self.menuFileSave)
        self.File.AppendItem(self.menuFileFlush)
        self.File.AppendItem(self.menuFileConsole)
        self.File.AppendItem(self.menuFileQuit)
        
        self.MenuBar.Append(self.File, "File")
        self.Bind(wx.EVT_MENU,self.OnOpenConsole,self.menuFileConsole)
        self.Bind(wx.EVT_MENU,self.OnConfigOpen,self.menuFileOpen)
        self.Bind(wx.EVT_MENU,self.OnConfigSave,self.menuFileSave)
        self.Bind(wx.EVT_MENU,self.OnFlushTemporaryFolder,self.menuFileFlush)
        self.Bind(wx.EVT_MENU,self.OnQuit,self.menuFileQuit)
        
        self.Type = wx.Menu()
        self.TypeRecip = wx.MenuItem(self.Type, -1, "Recip", "", wx.ITEM_RADIO)
        self.TypeScroll = wx.MenuItem(self.Type, -1, "Scroll", "", wx.ITEM_RADIO)
        self.TypeCompressor = wx.MenuItem(self.Type, -1, "Compressor mode", "", wx.ITEM_RADIO)
        self.TypeExpander = wx.MenuItem(self.Type, -1, "Expander mode", "", wx.ITEM_RADIO)
        self.TypeCompressor.Enable(False)
        self.TypeExpander.Enable(False)
        self.Type.AppendItem(self.TypeRecip)
        self.Type.AppendItem(self.TypeScroll)
        self.Type.AppendSeparator()
        self.Type.AppendItem(self.TypeCompressor)
        self.Type.AppendItem(self.TypeExpander)
        self.MenuBar.Append(self.Type, "Type")
        
        
        self.PluginsMenu = wx.Menu()
        #self.load_plugins(self.PluginsMenu)
        self.MenuBar.Append(self.PluginsMenu, "Plugins")
        
        
        if self.config_parser.get('Globals', 'Type') == 'recip':
            self.TypeRecip.Check(True)
        else:
            self.TypeScroll.Check(True)
            
        self.Bind(wx.EVT_MENU,self.OnChangeSimType,self.TypeScroll)
        self.Bind(wx.EVT_MENU,self.OnChangeSimType,self.TypeRecip)
        
        self.Solve = wx.Menu()
        self.SolveSolve = wx.MenuItem(self.Solve, -1, "Solve\tF5", "", wx.ITEM_NORMAL)
        self.Solve.AppendItem(self.SolveSolve)
        self.MenuBar.Append(self.Solve, "Solve")
        self.Bind(wx.EVT_MENU, self.OnStart, self.SolveSolve)
        
        self.Help = wx.Menu()
        #self.HelpHelp = wx.MenuItem(self.Help, -1, "Help...\tCtrl+H", "", wx.ITEM_NORMAL)
        self.HelpAbout = wx.MenuItem(self.Help, -1, "About", "", wx.ITEM_NORMAL)
        #self.Help.AppendItem(self.HelpHelp)
        self.Help.AppendItem(self.HelpAbout)
        self.MenuBar.Append(self.Help, "Help")
        #self.Bind(wx.EVT_MENU, lambda event: self.Destroy(), self.HelpHelp)
        self.Bind(wx.EVT_MENU, self.OnAbout, self.HelpAbout)
        
        #Actually set it
        self.SetMenuBar(self.MenuBar)        
        
    ################################
    #         Event handlers       #
    ################################
    
    def OnOpenConsole(self, event):
        frm = wx.Frame(None)
        from wx.py.crust import Crust
        console = Crust(frm, intro = 'Welcome to the debug console within PDSim', locals = locals())
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(console,1,wx.EXPAND)
        frm.SetSizer(sizer)
        frm.Show()
        
    def OnConfigOpen(self,event):
        FD = wx.FileDialog(None,"Load Configuration file",defaultDir='configs',
                           style=wx.FD_OPEN)
        if wx.ID_OK==FD.ShowModal():
            file_path=FD.GetPath()
            #Now rebuild the GUI using the desired configuration file
            self.rebuild(file_path)
        FD.Destroy()
        
    def OnConfigSave(self,event):
        FD = wx.FileDialog(None,"Save Configuration file",defaultDir='configs',
                           style=wx.FD_SAVE|wx.FD_OVERWRITE_PROMPT)
        if wx.ID_OK==FD.ShowModal():
            file_path=FD.GetPath()
            print 'Writing configuration file to ', file_path   
            #Build the config file entry
            string_list = []
            
            #Based on mode selected in menu select type to be written to file
            if self.TypeRecip.IsChecked():
                Type = 'recip'
            elif self.TypeScroll.IsChecked():
                Type = 'scroll'
            else:
                raise ValueError
            
            if self.TypeCompressor.IsChecked():
                mode = 'compressor'
            elif self.TypeExpander.IsChecked():
                mode = 'expander'
            else:
                raise ValueError
            
            #Header information
            header_string_template = textwrap.dedent(
                 """
                 [Globals]
                 Type = {CompressorType}
                 Mode = {Mode}
                 """
                 ) 
            terms = dict(CompressorType = Type, Mode = mode)
            header_string = header_string_template.format(**terms)

            string_list.append(unicode(header_string,'latin-1'))
            
            #Do all the "conventional" panels 
            for TB in self.MTB.Children:
                
                #Skip anything that isnt a toolbook
                if not isinstance(TB,wx.Toolbook):
                    continue
                
                #Loop over the panels that are in the toolbook
                for panel in TB.Children:
                    
                          
                    #Skip any panels that do not subclass PDPanel
                    if not isinstance(panel,pdsim_panels.PDPanel):
                        continue
                    
                    #Collect the string for writing to file
                    panel_string = panel.prep_for_configfile()
                    if isinstance(panel_string,str):
                        string_list.append(unicode(panel_string,'latin-1'))
                    elif isinstance(panel_string,unicode):
                        #Convert to a string
                        panel_string = unicode.decode(panel_string,'latin-1')
                        string_list.append(panel_string)
            
            for plugin in self.plugins_list:
                pass
#                if plugin.is_activated():
#                    if hasattr(plugin, ''):
#                        pass
                    
            fp = codecs.open(file_path,'w',encoding = 'latin-1')
            fp.write(u'\n'.join(string_list))
            fp.close()
        FD.Destroy()
        
    def OnStart(self, event):
        """
        Runs the primary inputs without applying the parametric table inputs
        """
        self.MTB.SetSelection(2)
        if self.SimType == 'recip':
            self.recip = self.build_recip()
            self.recip.run_index = 1
            self.run_simulation(self.recip)
        
        elif self.SimType == 'scroll':
            self.scroll = self.build_scroll()
            self.scroll.run_index = 1
            self.run_simulation(self.scroll)
            
    def OnStop(self, event):
        """Stop Computation."""
        if self.WTM is not None:
            self.WTM.abort()
        
    def OnQuit(self, event):
        self.Close()
        
    def OnIdle(self, event):
        """
        Do the things that are needed when the GUI goes idle
        
        This is only run every once in a while (see __init__) for performance-sake 
        """
        
        #Add results from the pipe to the GUI
        if not self.results_list.empty():
            print 'readying to get simulation'
            sim = self.results_list.get()
            print 'got a simulation'
            
            more_terms = []
            
            #Collect terms from the panels if any have them
            for TB in [self.MTB.InputsTB, self.MTB.SolverTB]:
                more_terms += TB.collect_output_terms()
                
            #Allow the plugins to post-process the results
            for plugin in self.plugins_list:
                if plugin.is_activated():
                    plugin.post_process(sim)
                    more_terms += plugin.collect_output_terms()
            
            #Add all the terms to the output table        
            self.MTB.OutputsTB.add_output_terms(more_terms)
                
#            from plugins.HDF5_plugin import HDF5Writer
#            HDF5 = HDF5Writer()
#            HDF5.write_to_file(sim,'sim.hd5')
            
            self.MTB.OutputsTB.plot_outputs(sim)
            self.MTB.OutputsTB.DataPanel.add_runs([sim])
            self.MTB.OutputsTB.DataPanel.rebuild()
            
            #Check whether there are no more results to be processed and threads list is empty
            #This means the manager has completed its work - reset it
            if self.results_list.empty() and not self.WTM.threadsList:
                self.WTM = None
        
        if self.results_list.empty() and self.WTM is not None and not self.WTM.threadsList:
            self.WTM = None
    
    def OnAbout(self, event = None):
        if "unicode" in wx.PlatformInfo:
            wx_unicode = '\nwx Unicode support: True\n'
        else:
            wx_unicode = '\nwx Unicode support: False\n'
        import CoolProp
        info = wx.AboutDialogInfo()
        info.Name = "PDSim GUI"
        info.Version = PDSim.__version__
        info.Copyright = "(C) 2012 Ian Bell"
        info.Description = wordwrap(
            "A graphical user interface for the PDSim model\n\n"+
            "wx version: "+wx.__version__+
            wx_unicode+
            "CoolProp version: "+CoolProp.__version__,
            350, wx.ClientDC(self))
        info.WebSite = ("http://pdsim.sourceforge.net", "PDSim home page")
        info.Developers = [ "Ian Bell", "Craig Bradshaw"]

        # Then we call wx.AboutBox giving it that info object
        wx.AboutBox(info)
        
    def OnChangeSimType(self, event):  
        if self.TypeScroll.IsChecked():
            print 'Scroll-type compressor'
            self.rebuild(default_configs.get_scroll_defaults())
        elif self.TypeRecip.IsChecked():
            print 'Recip-type compressor'
            self.rebuild(default_configs.get_recip_defaults())
        
    def OnFlushTemporaryFolder(self, events):
        """
        Event that fires on menu item to flush out temporary files.
        
        Checks to see if temp folder exists, if so, removes it
        """
        import shutil, glob
        home = os.getenv('USERPROFILE') or os.getenv('HOME')
        temp_folder = os.path.join(home,'.pdsim-temp')
        
        if os.path.exists(temp_folder):
            N = len(glob.glob(os.path.join(temp_folder,'*.*')))
            dlg = wx.MessageDialog(None,'There are '+str(N)+' files in the temporary folder.\n\nPress Ok to remove all the temporary files',style = wx.OK|wx.CANCEL)
            if dlg.ShowModal() == wx.ID_OK:    
                shutil.rmtree(temp_folder)
                print 'removed the folder',temp_folder 
            dlg.Destroy()
        else:
            dlg = wx.MessageDialog(None,'Temporary folder does not exist', style = wx.OK)
            dlg.ShowModal()
            dlg.Destroy()
    

class MySplashScreen(wx.SplashScreen):
    """
    Create a splash screen widget.
    """
    def __init__(self, parent=None):
        # This is a recipe to a the screen.
        # Modify the following variables as necessary.
        img = wx.Image(name = os.path.join("imgs","PDSim_logo.png"))
        width, height = img.GetWidth(), img.GetHeight()
        width *= 0.5
        height *= 0.5
        aBitmap = img.Rescale(width,height).ConvertToBitmap()
        splashStyle = wx.SPLASH_CENTRE_ON_SCREEN | wx.SPLASH_TIMEOUT
        splashDuration = 2000 # milliseconds
        # Call the constructor with the above arguments in exactly the
        # following order.
        wx.SplashScreen.__init__(self, aBitmap, splashStyle,
                                 splashDuration, parent)
        self.Bind(wx.EVT_CLOSE, self.OnExit)

        wx.Yield()

    def OnExit(self, evt):
        self.Hide()
        evt.Skip()  # Make sure the default handler runs too...
                    
if __name__ == '__main__':
    # The following line is required to allow cx_Freeze 
    # to package multiprocessing properly.  Must be the first line 
    # after if __name__ == '__main__':
    freeze_support()
    
    app = wx.App(False)
    
    
    if '--nosplash' not in sys.argv:
        Splash=MySplashScreen()
        Splash.Show()
        time.sleep(2.0)
    
    frame = MainFrame() 
    frame.Show(True) 
    
    app.MainLoop()