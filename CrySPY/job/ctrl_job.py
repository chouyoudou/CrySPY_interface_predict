'''
Control jobs
'''

import itertools
import os
import shutil
import subprocess

import numpy as np

from ..BO.select_descriptor import select_descriptor
from ..BO import bo_next_select
from ..EA import ea_next_gen
from ..gen_struc.struc_util import out_poscar, out_cif
from ..interface import select_code
from ..IO import read_input as rin
from ..IO import change_input, io_stat, pkl_data
from ..IO.out_results import out_rslt
from ..IO.out_results import out_laqa_status, out_laqa_step, out_laqa_score
from ..IO.out_results import out_laqa_energy, out_laqa_bias
from ..LAQA.calc_score import calc_laqa_bias
from ..LAQA import laqa_next_selection


class Ctrl_job:

    def __init__(self, stat, init_struc_data):
        self.stat = stat
        self.init_struc_data = init_struc_data
        self.opt_struc_data = pkl_data.load_opt_struc()
        self.rslt_data = pkl_data.load_rslt()
        self.recheck = False
        self.logic_next = False
        # ---------- for each algorithm
        if rin.algo == 'RS':
            self.id_queueing, self.id_running = pkl_data.load_rs_id()
        elif rin.algo == 'BO':
            (self.n_selection, self.id_queueing,
             self.id_running, self.id_select_hist) = pkl_data.load_bo_id()
            (self.init_dscrpt_data, self.opt_dscrpt_data,
             self.bo_mean, self.bo_var,
             self.bo_score) = pkl_data.load_bo_data()
        elif rin.algo == 'LAQA':
            (self.id_queueing, self.id_running,
             self.id_select_hist) = pkl_data.load_laqa_id()
            (self.tot_step_select, self.laqa_step,
             self.laqa_struc, self.laqa_energy,
             self.laqa_bias, self.laqa_score) = pkl_data.load_laqa_data()
        elif rin.algo == 'EA':
            (self.gen, self.id_queueing,
             self.id_running) = pkl_data.load_ea_id()
            # do not have to load ea_data here.
            # ea_data is used only in ea_next_gen.py
        # ---------- for option
        if rin.kpt_flag:
            self.kpt_data = pkl_data.load_kpt()
        if rin.energy_step_flag:
            self.energy_step_data = pkl_data.load_energy_step()
        if rin.struc_step_flag:
            self.struc_step_data = pkl_data.load_struc_step()
        if rin.fs_step_flag:
            self.fs_step_data = pkl_data.load_fs_step()

    def check_job(self):
        # ---------- option: recalc
        if rin.recalc:
            self.set_recalc()
        # ---------- temporarily append
        self.tmp_running = self.id_running[:]    # shallow copy
        self.tmp_queueing = self.id_queueing[:]
        if not rin.stop_next_struc:    # option: stop_next_struc
            while len(self.tmp_running) < rin.njob and self.tmp_queueing:
                self.tmp_running.append(self.tmp_queueing.pop(0))
        # ---------- initialize
        self.stage_stat = {}    # key: Structure ID
        self.job_stat = {}
        # ---------- check job status
        for cid in self.tmp_running:
            # ------ mkdir
            if not os.path.isdir('work/{:06}'.format(cid)):
                os.mkdir('work/{:06}'.format(cid))
            # ------ check stat_job file
            stat_path = 'work/{:06}'.format(cid) + '/stat_job'
            try:
                with open(stat_path, 'r') as fstat:
                    istat = fstat.readline()    # id
                    sstat = fstat.readline()    # stage
                    jstat = fstat.readline()    # submitted or done or ...
                self.stage_stat[cid] = int(sstat.split()[0])
                if not cid == int(istat.split()[0]):
                    raise SystemExit('ID is wrong in work/{:06}'.format(cid))
                self.stage_stat[cid] = int(sstat.split()[0])
                if jstat[0:3] == 'sub':
                    self.job_stat[cid] = 'submitted'
                elif jstat[0:4] == 'done':
                    self.job_stat[cid] = 'done'
                elif jstat[0:4] == 'skip':
                    self.job_stat[cid] = 'skip'
                else:
                    self.job_stat[cid] = 'else'
            except IOError:
                self.stage_stat[cid] = 'no_file'
                self.job_stat[cid] = 'no_file'

    def set_recalc(self):
        # ---------- check id
        for tid in rin.recalc:
            if tid not in self.opt_struc_data:
                raise ValueError('ID {} has not yet been calculated'.format(
                    tid))
        # ---------- append IDs to the head of id_queueing
        self.id_queueing = rin.recalc + self.id_queueing
        # ---------- log and out
        print('# -- Recalc')
        print('Append {} to the head of id_queueing'.format(rin.recalc))
        with open('cryspy.out', 'a') as fout:
            fout.write('\n# -- Recalc\n')
            fout.write('Append {} to the head of id_queueing\n\n'.format(
                rin.recalc))
        # ---------- clear recalc
        rin.recalc = []
        config = change_input.config_read()
        change_input.change_option(config, 'recalc', '')    # clear
        change_input.write_config(config)
        print('Clear recalc in cryspy.in')
        io_stat.set_input_common(self.stat, 'recalc', '')
        io_stat.write_stat(self.stat)

    def handle_job(self):
        print('\n# ---------- job status')
        for cid in self.tmp_running:
            # ---------- set work_path and current_id
            self.work_path = './work/{:06}/'.format(cid)
            self.current_id = cid
            # ---------- handle job
            if self.job_stat[cid] == 'submitted':
                print('ID {:>6}: still queueing or running'.format(cid))
            elif self.job_stat[cid] == 'done':
                self.ctrl_done()
            elif self.job_stat[cid] == 'skip':
                self.ctrl_skip()
            elif self.job_stat[cid] == 'else':
                raise ValueError('Wrong job_stat in {}. '.format(
                    self.work_path))
            elif self.job_stat[cid] == 'no_file':
                self.ctrl_next_struc()
            else:
                raise ValueError('Unexpected error in {}stat_job'.format(
                    self.work_path))

    def ctrl_done(self):
        self.current_stage = self.stage_stat[self.current_id]
        # ---------- log
        print('ID {0:>6}: Stage {1} Done!'.format(
            self.current_id, self.current_stage))
        # ---------- next stage
        if self.current_stage < rin.nstage:
            self.ctrl_next_stage()
        # ---------- collect result
        elif self.current_stage == rin.nstage:
            self.ctrl_collect()
        # ---------- error
        else:
            raise ValueError('Wrong stage in '+self.work_path+'stat_job')

    def ctrl_next_stage(self):
        # ---------- energy step
        if rin.energy_step_flag:
            self.energy_step_data = select_code.get_energy_step(
                self.energy_step_data, self.current_id, self.work_path)
        # ---------- struc step
        if rin.struc_step_flag:
            self.struc_step_data = select_code.get_struc_step(
                self.struc_step_data, self.current_id, self.work_path)
        # ---------- fs step
        if rin.fs_step_flag:
            self.fs_step_data = select_code.get_fs_step(
                self.fs_step_data, self.current_id, self.work_path)
        # ---------- next stage
        if rin.kpt_flag:
            skip_flag, self.kpt_data = select_code.next_stage(
                self.current_stage, self.work_path,
                self.kpt_data, self.current_id)
        else:
            skip_flag = select_code.next_stage(self.current_stage,
                                               self.work_path)
        # ---------- skip
        if skip_flag:
            self.ctrl_skip()
            return
        # ---------- prepare jobfile
        self.prepare_jobfile()
        # ---------- submit
        self.submit_next_stage()

    def submit_next_stage(self):
        # ---------- submit job
        os.chdir(self.work_path)    # cd work_path
        with open('stat_job', 'w') as fwstat:
            fwstat.write('{:<6}    # Structure ID\n'.format(self.current_id))
            fwstat.write('{:<6}    # Stage\n'.format(self.current_stage + 1))
            fwstat.write('submitted\n')
        with open('sublog', 'w') as logf:
            subprocess.Popen([rin.jobcmd, rin.jobfile],
                             stdout=logf, stderr=logf)
        os.chdir('../../')    # go back to ..
        # ---------- save status
        io_stat.set_stage(self.stat, self.current_id, self.current_stage + 1)
        io_stat.write_stat(self.stat)
        # ---------- log
        print('    submitted job, ID {0:>6} Stage {1}'.format(
            self.current_id, self.current_stage + 1))

    def ctrl_collect(self):
        # ---------- energy step
        if rin.energy_step_flag:
            self.energy_step_data = select_code.get_energy_step(
                self.energy_step_data, self.current_id, self.work_path)
        # ---------- struc step
        if rin.struc_step_flag:
            self.struc_step_data = select_code.get_struc_step(
                self.struc_step_data, self.current_id, self.work_path)
        # ---------- fs step
        if rin.fs_step_flag:
            self.fs_step_data = select_code.get_fs_step(
                self.fs_step_data, self.current_id, self.work_path)
        # ---------- each algo
        if rin.algo == 'RS':
            self.ctrl_collect_rs()
        elif rin.algo == 'BO':
            self.ctrl_collect_bo()
        elif rin.algo == 'LAQA':
            self.ctrl_collect_laqa()
        elif rin.algo == 'EA':
            self.ctrl_collect_ea()
        else:
            raise ValueError('Error, algo')
        # ---------- move to fin
        if rin.algo == 'LAQA':
            if self.fin_laqa:
                self.mv_fin()
            else:
                os.rename(self.work_path+'stat_job',
                          self.work_path+'prev_stat_job')
        else:
            self.mv_fin()
        # ---------- update status
        self.update_status(operation='fin')
        # ---------- recheck
        self.recheck = True

    def ctrl_collect_rs(self):
        # ---------- get opt data
        opt_struc, energy, magmom, check_opt = \
            select_code.collect(self.current_id, self.work_path)
        with open('cryspy.out', 'a') as fout:
            fout.write('Done! ID {0:>6}: E = {1} eV/atom\n'.format(
                self.current_id, energy))
        print('    collect results: E = {0} eV/atom'.format(energy))
        # ---------- register opt_struc
        spg_sym, spg_num, spg_sym_opt, spg_num_opt = self.regist_opt(opt_struc)
        # ---------- save rslt
        self.rslt_data.loc[self.current_id] = [spg_num, spg_sym,
                                               spg_num_opt, spg_sym_opt,
                                               energy, magmom, check_opt]
        pkl_data.save_rslt(self.rslt_data)
        out_rslt(self.rslt_data)

    def ctrl_collect_bo(self):
        # ---------- get opt data
        opt_struc, energy, magmom, check_opt = \
            select_code.collect(self.current_id, self.work_path)
        with open('cryspy.out', 'a') as fout:
            fout.write('Done! ID {0:>6}: E = {1} eV/atom\n'.format(
                self.current_id, energy))
        print('    collect results: E = {0} eV/atom'.format(energy))
        # ---------- register opt_struc
        spg_sym, spg_num, spg_sym_opt, spg_num_opt = self.regist_opt(opt_struc)
        # ---------- save rslt
        self.rslt_data.loc[self.current_id] = [self.n_selection,
                                               spg_num, spg_sym,
                                               spg_num_opt, spg_sym_opt,
                                               energy, magmom, check_opt]
        pkl_data.save_rslt(self.rslt_data)
        out_rslt(self.rslt_data)
        # ---------- success
        if opt_struc is not None:
            # ------ calc descriptor for opt sturcture
            tmp_dscrpt = select_descriptor({self.current_id: opt_struc})
            # ------ update descriptors
            self.opt_dscrpt_data.update(tmp_dscrpt)
        # ---------- error
        else:
            # ------ update descriptors and non_error_id
            self.opt_dscrpt_data[self.current_id] = None
        # ---------- save bo_data
        bo_data = (self.init_dscrpt_data, self.opt_dscrpt_data,
                   self.bo_mean, self.bo_var, self.bo_score)
        pkl_data.save_bo_data(bo_data)

    def ctrl_collect_laqa(self):
        # ---------- flag for finish
        self.fin_laqa = False
        # ---------- get opt data
        opt_struc, energy, magmom, check_opt = \
            select_code.collect(self.current_id, self.work_path)
        # ---------- total step and laqa_step
        #     fs_step_data[0] <-- force_step_data
        #     force_step_data[key][stage][step][atom]
        if self.fs_step_data[0][self.current_id][-1] is None:
            self.laqa_step[self.current_id].append(0)
        else:
            self.tot_step_select[-1] += len(
                self.fs_step_data[0][self.current_id][-1])
            self.laqa_step[self.current_id].append(
                len(self.fs_step_data[0][self.current_id][-1]))
        # ------ save status
        io_stat.set_common(self.stat, 'total_step', sum(self.tot_step_select))
        io_stat.write_stat(self.stat)
        # ---------- append laqa struc
        self.laqa_struc[self.current_id].append(opt_struc)
        # ---------- append laqa energy
        self.laqa_energy[self.current_id].append(energy)
        # ---------- append laqa bias
        #     fs_step_data[0] <-- force_step_data
        #     force_step_data[key][stage][step][atom]
        tmp_laqa_bias = calc_laqa_bias(
            self.fs_step_data[0][self.current_id][-1], c=rin.weight_laqa)
        self.laqa_bias[self.current_id].append(tmp_laqa_bias)
        # ---------- append laqa score
        if check_opt == 'done' or np.isnan(energy) or np.isnan(tmp_laqa_bias):
            self.laqa_score[self.current_id].append(-float('inf'))
        else:
            self.laqa_score[self.current_id].append(-energy + tmp_laqa_bias)
        # ---------- save laqa data
        laqa_data = (self.tot_step_select, self.laqa_step, self.laqa_struc,
                     self.laqa_energy, self.laqa_bias, self.laqa_score)
        pkl_data.save_laqa_data(laqa_data)
        # ---------- out laqa data
        out_laqa_status(self.laqa_step, self.laqa_score,
                        self.laqa_energy, self.laqa_bias)
        out_laqa_step(self.laqa_step)
        out_laqa_score(self.laqa_score)
        out_laqa_energy(self.laqa_energy)
        out_laqa_bias(self.laqa_bias)
        pkl_data.save_laqa_data(laqa_data)
        # ---------- case of 'done' or error
        if check_opt == 'done' or np.isnan(energy) or np.isnan(tmp_laqa_bias):
            self.fin_laqa = True
            with open('cryspy.out', 'a') as fout:
                fout.write('Done! ID {0:>6}: E = {1} eV/atom\n'.format(
                    self.current_id, energy))
            print('    collect results: E = {0} eV/atom'.format(energy))
            # ------ register opt_struc
            (spg_sym, spg_num,
             spg_sym_opt, spg_num_opt) = self.regist_opt(opt_struc)
            # ------ save rslt
            self.rslt_data.loc[self.current_id] = [spg_num, spg_sym,
                                                   spg_num_opt, spg_sym_opt,
                                                   energy, magmom, check_opt]
            pkl_data.save_rslt(self.rslt_data)
            out_rslt(self.rslt_data)

    def ctrl_collect_ea(self):
        # ---------- get opt data
        opt_struc, energy, magmom, check_opt = \
            select_code.collect(self.current_id, self.work_path)
        with open('cryspy.out', 'a') as fout:
            fout.write('Done! Structure ID {0:>6}: E = {1} eV/atom\n'.format(
                self.current_id, energy))
        print('    collect results: E = {0} eV/atom'.format(energy))
        # ---------- register opt_struc
        spg_sym, spg_num, spg_sym_opt, spg_num_opt = self.regist_opt(opt_struc)
        # ---------- save rslt
        self.rslt_data.loc[self.current_id] = [self.gen,
                                               spg_num, spg_sym,
                                               spg_num_opt, spg_sym_opt,
                                               energy, magmom, check_opt]
        pkl_data.save_rslt(self.rslt_data)
        out_rslt(self.rslt_data)
        # ------ success
        if opt_struc is not None:
            ea_id_data = (self.gen, self.id_queueing, self.id_running)
            pkl_data.save_ea_id(ea_id_data)

    def regist_opt(self, opt_struc):
        '''
        Common part in ctrl_collect_*
        '''
        # ---------- get initial spg info
        try:
            spg_sym, spg_num = self.init_struc_data[
                self.current_id].get_space_group_info(symprec=rin.symprec)
        except TypeError:
            spg_num = 0
            spg_sym = None
        # ---------- success
        if opt_struc is not None:
            # ------ get opt spg info
            try:
                spg_sym_opt, spg_num_opt = opt_struc.get_space_group_info(
                    symprec=rin.symprec)
            except TypeError:
                spg_num_opt = 0
                spg_sym_opt = None
            # ------ out opt_struc
            out_poscar(opt_struc, self.current_id, './data/opt_POSCARS')
            try:
                out_cif(opt_struc, self.current_id, self.work_path,
                        './data/opt_CIFS.cif', rin.symprec)
            except TypeError:
                print('failed to write opt_CIF')
        # ---------- error
        else:
            spg_num_opt = 0
            spg_sym_opt = None
        # ---------- register opt_struc
        self.opt_struc_data[self.current_id] = opt_struc
        pkl_data.save_opt_struc(self.opt_struc_data)
        # ---------- return
        return spg_sym, spg_num, spg_sym_opt, spg_num_opt

    def ctrl_next_struc(self):
        # ---------- RS
        if rin.algo == 'RS':
            next_struc_data = self.init_struc_data[self.current_id]
        # ---------- BO
        elif rin.algo == 'BO':
            next_struc_data = self.init_struc_data[self.current_id]
        # ---------- LAQA
        elif rin.algo == 'LAQA':
            if self.laqa_struc[self.current_id]:    # vacant list?
                next_struc_data = self.laqa_struc[self.current_id][-1]
            else:
                next_struc_data = self.init_struc_data[self.current_id]
        # ---------- EA
        elif rin.algo == 'EA':
            next_struc_data = self.init_struc_data[self.current_id]
        # ---------- algo is wrong
        else:
            raise ValueError('Error, algo')
        # ---------- common part
        # ------ in case there is no initial strucure data
        if next_struc_data is None:
            print('ID {:>6}: initial structure is None'.format(
                self.current_id))
            self.ctrl_skip()
        # ------ normal initial structure data
        else:
            # -- prepare input files for structure optimization
            if rin.kpt_flag:
                self.kpt_data = select_code.next_struc(next_struc_data,
                                                       self.current_id,
                                                       self.work_path,
                                                       self.kpt_data)
            else:
                select_code.next_struc(next_struc_data, self.current_id,
                                       self.work_path)
            # -- prepare jobfile
            self.prepare_jobfile()
            # -- submit
            self.submit_next_struc()
            print('ID {:>6}: submit job, Stage 1'.format(self.current_id))
            # -- update status
            self.update_status(operation='submit')

    def submit_next_struc(self):
        # ---------- submit job
        os.chdir(self.work_path)    # cd work_path
        with open('stat_job', 'w') as fwstat:
            fwstat.write('{:<6}    # Structure ID\n'.format(self.current_id))
            fwstat.write('{:<6}    # Stage\n'.format(1))
            fwstat.write('submitted\n')
        with open('sublog', 'w') as logf:
            subprocess.Popen([rin.jobcmd, rin.jobfile],
                             stdout=logf, stderr=logf)
        os.chdir('../../')    # go back to csp root dir

    def ctrl_skip(self):
        # ---------- log and out
        with open('cryspy.out', 'a') as fout:
            fout.write('ID {:>6}: Skip\n'.format(self.current_id))
        print('ID {:>6}: Skip'.format(self.current_id))
        # ---------- get initial spg info
        if self.init_struc_data[self.current_id] is None:
            spg_sym = None
            spg_num = 0
        else:
            try:
                spg_sym, spg_num = self.init_struc_data[
                    self.current_id].get_space_group_info(symprec=rin.symprec)
            except TypeError:
                spg_num = 0
                spg_sym = None
        # ---------- 'skip' for rslt
        spg_num_opt = 0
        spg_sym_opt = None
        energy = np.nan
        magmom = np.nan
        check_opt = 'skip'
        # ---------- register opt_struc
        self.opt_struc_data[self.current_id] = None
        pkl_data.save_opt_struc(self.opt_struc_data)
        # ---------- RS
        if rin.algo == 'RS':
            # ------ save rslt
            self.rslt_data.loc[self.current_id] = [spg_num, spg_sym,
                                                   spg_num_opt, spg_sym_opt,
                                                   energy, magmom, check_opt]
            pkl_data.save_rslt(self.rslt_data)
            out_rslt(self.rslt_data)
        # ---------- BO
        elif rin.algo == 'BO':
            # ------ save rslt
            self.rslt_data.loc[self.current_id] = [self.n_selection,
                                                   spg_num, spg_sym,
                                                   spg_num_opt, spg_sym_opt,
                                                   energy, magmom, check_opt]
            pkl_data.save_rslt(self.rslt_data)
            out_rslt(self.rslt_data)
            # ------ update descriptors
            self.opt_dscrpt_data[self.current_id] = None
            # ------ save
            bo_id_data = (self.n_selection, self.id_queueing,
                          self.id_running, self.id_select_hist)
            pkl_data.save_bo_id(bo_id_data)
            bo_data = (self.init_dscrpt_data, self.opt_dscrpt_data,
                       self.bo_mean, self.bo_var, self.bo_score)
            pkl_data.save_bo_data(bo_data)
        # ---------- LAQA
        elif rin.algo == 'LAQA':
            # ------ save rslt
            self.rslt_data.loc[self.current_id] = [spg_num, spg_sym,
                                                   spg_num_opt, spg_sym_opt,
                                                   energy, magmom, check_opt]
            pkl_data.save_rslt(self.rslt_data)
            out_rslt(self.rslt_data)
            # ---------- laqa data
            self.laqa_step[self.current_id].append(0)
            self.laqa_struc[self.current_id].append(None)
            self.laqa_energy[self.current_id].append(energy)
            self.laqa_bias[self.current_id].append(np.nan)
            self.laqa_score[self.current_id].append(-float('inf'))
            # ---------- save laqa data
            laqa_data = (self.tot_step_select, self.laqa_step, self.laqa_struc,
                         self.laqa_energy, self.laqa_bias, self.laqa_score)
            pkl_data.save_laqa_data(laqa_data)
            # ---------- out laqa data
            out_laqa_status(self.laqa_step, self.laqa_score,
                            self.laqa_energy, self.laqa_bias)
            out_laqa_step(self.laqa_step)
            out_laqa_score(self.laqa_score)
            out_laqa_energy(self.laqa_energy)
            out_laqa_bias(self.laqa_bias)
        # ---------- EA
        elif rin.algo == 'EA':
            self.rslt_data.loc[self.current_id] = [self.gen,
                                                   spg_num, spg_sym,
                                                   spg_num_opt, spg_sym_opt,
                                                   energy, magmom, check_opt]
            pkl_data.save_rslt(self.rslt_data)
            out_rslt(self.rslt_data)
        # ---------- move to fin
        self.mv_fin()
        # ---------- update status
        self.update_status(operation='fin')
        # ---------- recheck
        self.recheck = True

    def update_status(self, operation):
        # ---------- update status
        if operation == 'submit':
            self.id_running.append(self.current_id)
            self.id_queueing.remove(self.current_id)
            io_stat.set_stage(self.stat, self.current_id, 1)
        elif operation == 'fin':
            if self.current_id in self.id_queueing:
                self.id_queueing.remove(self.current_id)
            if self.current_id in self.id_running:
                self.id_running.remove(self.current_id)
            io_stat.clean_id(self.stat, self.current_id)
        else:
            raise ValueError('operation is wrong')
        io_stat.set_id(self.stat, 'id_queueing', self.id_queueing)
        io_stat.write_stat(self.stat)
        # ---------- save id_data
        if rin.algo == 'RS':
            rs_id_data = (self.id_queueing, self.id_running)
            pkl_data.save_rs_id(rs_id_data)
        elif rin.algo == 'BO':
            bo_id_data = (self.n_selection, self.id_queueing,
                          self.id_running, self.id_select_hist)
            pkl_data.save_bo_id(bo_id_data)
        elif rin.algo == 'LAQA':
            laqa_id_data = (self.id_queueing, self.id_running,
                            self.id_select_hist)
            pkl_data.save_laqa_id(laqa_id_data)
        if rin.algo == 'EA':
            ea_id_data = (self.gen, self.id_queueing, self.id_running)
            pkl_data.save_ea_id(ea_id_data)

    def prepare_jobfile(self):
        if not os.path.isfile('./calc_in/' + rin.jobfile):
            raise IOError('Could not find ./calc_in' + rin.jobfile)
        with open('./calc_in/' + rin.jobfile, 'r') as f:
            lines = f.readlines()
        lines2 = []
        for line in lines:
            lines2.append(line.replace('CrySPY_ID', str(self.current_id)))
        with open(self.work_path + rin.jobfile, 'w') as f:
            f.writelines(lines2)

    def mv_fin(self):
        if not os.path.isdir('work/fin/{0:06}'.format(self.current_id)):
            shutil.move('work/{:06}'.format(self.current_id), 'work/fin/')
        else:    # rename for recalc
            for i in itertools.count(1):
                if not os.path.isdir('work/fin/{0:06}_{1}'.format(
                        self.current_id, i)):
                    shutil.move('work/{:06}'.format(self.current_id),
                                'work/fin/{0:06}_{1}'.format(
                                    self.current_id, i))
                    break

    def next_sg(self):
        '''
        next selection or generation
        '''
        if rin.algo == 'BO':
            self.next_select_BO()
        if rin.algo == 'LAQA':
            self.next_select_LAQA()
        if rin.algo == 'EA':
            self.next_gen_EA()

    def next_select_BO(self):
        # ---------- log and out
        with open('cryspy.out', 'a') as fout:
            fout.write('\nDone selection {}\n\n'.format(self.n_selection))
        print('\nDone selection {}\n'.format(self.n_selection))
        # ---------- done all structures
        if len(self.rslt_data) == rin.tot_struc:
            with open('cryspy.out', 'a') as fout:
                fout.write('\nDone all structures!\n')
            print('Done all structures!')
            os.remove('lock_cryspy')
            raise SystemExit()
        # ---------- check point 3
        if rin.stop_chkpt == 3:
            print('Stop at check point 3: BO is ready\n')
            os.remove('lock_cryspy')
            raise SystemExit()
        # ---------- max_select_bo
        if 0 < rin.max_select_bo <= self.n_selection:
            print('Reached max_select_bo: {}\n'.format(rin.max_select_bo))
            os.remove('lock_cryspy')
            raise SystemExit()
        # ---------- BO
        bo_data = (self.init_dscrpt_data, self.opt_dscrpt_data,
                   self.bo_mean, self.bo_var, self.bo_score)
        bo_id_data = (self.n_selection, self.id_queueing,
                      self.id_running, self.id_select_hist)
        bo_next_select.next_select(self.stat, self.rslt_data,
                                   bo_id_data, bo_data)

    def next_select_LAQA(self):
        # ---------- check point 3
        if rin.stop_chkpt == 3:
            print('\nStop at check point 3: LAQA is ready\n')
            os.remove('lock_cryspy')
            raise SystemExit()
        # ---------- selection of LAQA
        laqa_id_data = (self.id_queueing, self.id_running,
                        self.id_select_hist)
        laqa_data = (self.tot_step_select, self.laqa_step, self.laqa_struc,
                     self.laqa_energy, self.laqa_bias, self.laqa_score)
        laqa_next_selection.next_selection(self.stat, laqa_id_data, laqa_data)

    def next_gen_EA(self):
        # ---------- log and out
        with open('cryspy.out', 'a') as fout:
            fout.write('\nDone generation {}\n\n'.format(self.gen))
        print('\nDone generation {}\n'.format(self.gen))
        # ---------- check point 3
        if rin.stop_chkpt == 3:
            print('\nStop at check point 3: EA is ready\n')
            os.remove('lock_cryspy')
            raise SystemExit()
        # ---------- maxgen_ea
        if 0 < rin.maxgen_ea <= self.gen:
            print('\nReached maxgen_ea: {}\n'.format(rin.maxgen_ea))
            os.remove('lock_cryspy')
            raise SystemExit()
        # ---------- EA
        ea_id_data = (self.gen, self.id_queueing, self.id_running)
        ea_next_gen.next_gen(self.stat, self.init_struc_data,
                             self.opt_struc_data, self.rslt_data, ea_id_data)
