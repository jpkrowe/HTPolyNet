import logging
import os
import shutil
import numpy as np
from copy import deepcopy
from HTPolyNet.configuration import Configuration
from HTPolyNet.topology import select_topology_type_option
from HTPolyNet.topocoord import TopoCoord
import HTPolyNet.projectfilesystem as pfs
import HTPolyNet.software as software
from HTPolyNet.gromacs import insert_molecules, gmx_energy_trace, mdp_modify, mdp_get
import HTPolyNet.checkpoint as cp
from HTPolyNet.plot import trace
from HTPolyNet.molecule import Molecule, MoleculeDict, MoleculeList, is_reactant
from HTPolyNet.expandreactions import symmetry_expand_reactions, chain_expand_reactions
from HTPolyNet.curecontroller import CureController
from HTPolyNet.stringthings import my_logger

logger=logging.getLogger(__name__)

def logrotate(filename):
    if os.path.exists(filename):
        n=1
        while os.path.exists(f'#{n}#{filename}'):
            n+=1
        shutil.copyfile(filename,f'#{n}#{filename}')

class Runtime:
    ''' Class for a single HTPolyNet runtime session '''
    def __init__(self,cfgfile='',restart=False):
        my_logger(software.to_string(),logger.info)
        self.cfgfile=cfgfile
        if cfgfile=='':
            logger.error('HTPolyNet requires a configuration file.\n')
            raise RuntimeError('HTPolyNet requires a configuration file')
        logger.info(f'Configuration: {cfgfile}')
        self.cfg=Configuration.read(os.path.join(pfs.root(),cfgfile))
        software.set_gmx_preferences(self.cfg.parameters)
        self.TopoCoord=TopoCoord(system_name='htpolynet')
        self.cfg.parameters['restart']=restart
        if self.cfg.parameters['restart']:
            logger.info(f'Restarting in {pfs.proj()}')
        self.molecules:MoleculeDict={}
        self.cc=CureController(self.cfg.basedict)
        self.ncpu=self.cfg.parameters.get('ncpu',os.cpu_count())

    def generate_molecules(self,force_parameterization=False,force_checkin=False):
        GAFF_dict=self.cfg.parameters.get('GAFF',{})
        my_logger('Generating molecular templates',logger.info)
        self.molecules={}
        ''' configuration.parse() generated a list of Molecules implied by configuration; assume
            they are all unparameterized '''
        for mname,M in self.cfg.molecules.items():
            M.set_origin('unparameterized')
        cwd=pfs.go_to('molecules/parameterized')

        ''' Each molecule implied by the cfg is 'generated' here, either by
            reading from the library or direct parameterization.  In some cases,
            the molecule is to be generated by a reaction; if so, it's
            `generator` attribute will be a Reaction instance '''
        ess='' if len(self.cfg.molecules)==1 else 's'
        my_logger(f'{len(self.cfg.molecules)} molecule{ess} explicit in {self.cfgfile}',logger.info)
        ml=list(self.cfg.molecules.keys())
        my_logger(ml,logger.info)
        all_made=all([(m in self.molecules and M.get_origin()!='unparameterized') for m,M in self.cfg.molecules.items()])
        while not all_made:
            for mname,M in self.cfg.molecules.items():
                if mname not in self.molecules:
                    if self.generate_molecule(M,force_parameterization=force_parameterization,force_checkin=force_checkin):
                        self.molecules[mname]=M
                        logger.debug(f'Generated {mname}')
            all_made=all([(m in self.molecules and M.get_origin()!='unparameterized') for m,M in self.cfg.molecules.items()])

        ''' Generate all reactions and products that result from invoking symmetry '''
        symmetry_relateds=self.cfg.parameters.get('symmetry_equivalent_atoms',{})
        if not symmetry_relateds:
            constituents=self.cfg.parameters.get('constituents',{})
            if not constituents:
                raise Exception(f'Config file must have a "symmetry_equivalent_atoms" key if no "constituents" key is specified')
            for cname,crec in constituents.items():
                this_sr=crec.get('symmetry_equivalent_atoms',[])
                if len(this_sr)>0:
                    symmetry_relateds[cname]=this_sr
        if len(symmetry_relateds)>0:
            new_reactions,new_molecules=symmetry_expand_reactions(self.cfg.reactions,symmetry_relateds)
            ess='' if len(new_molecules)==1 else 's'
            my_logger(f'{len(new_molecules)} molecule{ess} implied by symmetry-equivalent atoms',logger.info)
            ml=list(new_molecules.keys())
            my_logger(ml,logger.info)
            self.cfg.reactions.extend(new_reactions)
            make_molecules={k:v for k,v in new_molecules.items() if k not in self.molecules}
            for mname,M in make_molecules.items():
                self.generate_molecule(M,force_parameterization=force_parameterization,force_checkin=force_checkin)
                assert M.get_origin()!='unparameterized'
                self.molecules[mname]=M
                logger.debug(f'Generated {mname}')

        ''' Generate any required template products that result from reactions in which the bond generated creates
            dihedrals that span more than just the two monomers that are connected '''
        new_reactions,new_molecules=chain_expand_reactions(self.molecules)
        if len(new_molecules)>0:
            ess='' if len(new_molecules)==1 else 's'
            my_logger(f'{len(new_molecules)} molecule{ess} implied by chaining',logger.info)
            ml=list(new_molecules.keys())
            my_logger(ml,logger.info)
            self.cfg.reactions.extend(new_reactions)
            make_molecules={k:v for k,v in new_molecules.items() if k not in self.molecules}
            for mname,M in make_molecules.items():
                # logger.debug(f'Generating {mname}:')
                self.generate_molecule(M,force_parameterization=force_parameterization,force_checkin=force_checkin)
                assert M.get_origin()!='unparameterized'
                self.molecules[mname]=M
                logger.debug(f'Generated {mname}')

        for M in self.molecules:
            self.molecules[M].is_reactant=is_reactant(M,self.cfg.reactions,stage='cure')

        resolve_type_discrepancies=GAFF_dict.get('resolve_type_discrepancies',[])
        if not resolve_type_discrepancies:
            resolve_type_discrepancies=self.cfg.parameters.get('resolve_type_discrepancies',[])
        if resolve_type_discrepancies:
            for resolve_directive in resolve_type_discrepancies:
                logger.info(f'Resolving type discrepancies for directive {resolve_directive}')
                self.type_consistency_check(typename=resolve_directive['typename'],funcidx=resolve_directive.get('funcidx',4),selection_rule=resolve_directive['rule'])

        ess='' if len(self.molecules)==1 else 's'
        my_logger(f'Generated {len(self.molecules)} molecule template{ess}',logger.info)
        my_logger(f'Initial composition is {", ".join([(x["molecule"]+" "+str(x["count"])) for x in self.cfg.initial_composition])}',logger.info)
        self.cfg.calculate_maximum_conversion()
        my_logger(f'100% conversion is {self.cfg.maxconv} bonds',logger.info)

        logger.debug(f'Reaction bond(s) in each molecular template:')
        for M in self.molecules.values():
            if len(M.reaction_bonds)>0:
                logger.debug(f'Bond {M.name}:')
                for b in M.reaction_bonds:
                    logger.debug(f'   {str(b)}')

        logger.debug(f'Bond template(s) in each molecular template:')
        for M in self.molecules.values():
            if len(M.bond_templates)>0:
                logger.debug(f'Template {M.name}:')
                for b in M.bond_templates:
                    logger.debug(f'   {str(b)}')

        for k,v in self.cfg.parameters.get('constituents',{}).items():
            relaxdict=v.get('relax',{})
            if relaxdict:
                self.molecules[k].relax(relaxdict)

    def generate_molecule_par(self,ML:MoleculeList,**kwargs):
        for M in ML:
            self.generate_molecule(M,**kwargs)

    def generate_molecule(self,M:Molecule,**kwargs):
        mname=M.name
        checkin=pfs.checkin
        # pfs.go_to(f'molecules/parameterized/work/{M.name}')
        force_parameterization=kwargs.get('force_parameterization',False)
        force_checkin=kwargs.get('force_checkin',False)
        if force_parameterization or not M.previously_parameterized():
            logger.debug(f'Parameterization of {mname} requested -- can we generate {mname}?')
            generatable=(not M.generator) or (all([m in self.molecules for m in M.generator.reactants.values()]))
            if generatable:
                logger.debug(f'Generating {mname}')
                M.generate(available_molecules=self.molecules,**self.cfg.parameters)
                for ex in ['mol2','top','itp','gro','grx']:
                    checkin(f'molecules/parameterized/{mname}.{ex}',overwrite=force_checkin)
#                    checkin(f'molecules/parameterized/work/{mname}/{mname}.{ex}',overwrite=force_checkin)
#                    shutil.copy(f'molecules/parameterized/{mname}.{ex}','../../')
                M.set_origin('newly parameterized')
            else:
                logger.debug(f'...no, did not generate {mname}')
                logger.debug(f'not ({mname}.generator) {bool(not M.generator)}')
                if M.generator:
                    logger.debug(f'reactants {list(M.generator.reactants.values())}')
                return False
        else:
            logger.debug(f'Fetching parameterized {mname}')
            for ex in ['mol2','top','itp','gro','grx']:
                pfs.checkout(f'molecules/parameterized/{mname}.{ex}')
            M.load_top_gro(f'{mname}.top',f'{mname}.gro',mol2filename=f'{mname}.mol2',wrap_coords=False)
            M.TopoCoord.read_gro_attributes(f'{mname}.grx')
            # logger.debug(f'{M.name} box {M.TopoCoord.Coordinates.box}')
            M.set_sequence()
            if M.generator:
                M.prepare_new_bonds(available_molecules=self.molecules)
            M.set_origin('previously parameterized')

        M.generate_stereoisomers()

        return True

    def type_consistency_check(self,typename='dihedraltypes',funcidx=4,selection_rule='stiffest'):
        logger.debug(f'Consistency check of {typename} func {funcidx} on all {len(self.molecules)} molecules requested')
        mnames=list(self.molecules.keys())
        # checkin=pfs.checkin
        types_duplicated=[]
        for i in range(len(mnames)):
            logger.debug(f'{mnames[i]}...')
            for j in range(i+1,len(mnames)):
                mol1topo=self.molecules[mnames[i]].TopoCoord.Topology
                mol2topo=self.molecules[mnames[j]].TopoCoord.Topology
                this_duptypes=mol1topo.report_duplicate_types(mol2topo,typename=typename,funcidx=funcidx)
                logger.debug(f'...{mnames[j]} {len(this_duptypes)}')
                for d in this_duptypes:
                    if not d in types_duplicated:
                        types_duplicated.append(d)
        logger.debug(f'Duplicated {typename}: {types_duplicated}')
        options={}
        for t in types_duplicated:
            logger.debug(f'Duplicate {t}:')
            options[t]=[]
            for i in range(len(mnames)):
                moltopo=self.molecules[mnames[i]].TopoCoord.Topology
                this_type=moltopo.report_type(t,typename='dihedraltypes',funcidx=4)
                if len(this_type)>0 and not this_type in options[t]:
                    options[t].append(this_type)
                logger.debug(f'{mnames[i]} reports {this_type}')
            logger.debug(f'Conflicting options for this type: {options[t]}')
            selected_type=select_topology_type_option(options[t],typename,rule=selection_rule)
            logger.debug(f'Under selection rule "{selection_rule}", preferred type is {selected_type}')
            for i in range(len(mnames)):
                logger.debug(f'resetting {mnames[i]}')
                TC=self.molecules[mnames[i]].TopoCoord
                moltopo=TC.Topology
                moltopo.reset_type(typename,t,selected_type)
                # TC.write_top(f'{mnames[i]}.top')
                # checkin(f'molecules/parameterized/{mnames[i]}.top')

    def initialize_topology(self,inpfnm='init'):
        """Create a full gromacs topology that includes all directives necessary
            for an initial liquid simulation.  This will NOT use any #include's;
            all types will be explicitly in-lined.

        :param inpfnm: input file name prefix, defaults to 'init'
        :type inpfnm: str, optional
        """
        TC=self.TopoCoord
        if cp.passed('initialize_topology'): return
        cwd=pfs.go_to('systems/init')
        if os.path.isfile(f'{inpfnm}.top'):
            logger.debug(f'{inpfnm}.top already exists in {cwd} but we will rebuild it anyway!')

        ''' for each monomer named in the cfg, either parameterize it or fetch its parameterization '''
        already_merged=[]
        for item in self.cfg.initial_composition:
            M=self.molecules[item['molecule']]
            N=item['count']
            t=deepcopy(M.TopoCoord.Topology)
            logger.debug(f'Merging {N} copies of {M.name}\'s topology into global topology')
            t.adjust_charges(atoms=t.D['atoms']['nr'].to_list(),desired_charge=0.0,overcharge_threshhold=0.1,msg='')
            t.rep_ex(N)
            TC.Topology.merge(t)
            already_merged.append(M.name)
        for othermol,M in self.molecules.items():
            if not othermol in already_merged:
                logger.debug(f'Merging types from {othermol}\'s topology into global topology')
                TC.Topology.merge_types(M.TopoCoord.Topology)
        my_logger(f'Generated {inpfnm}.top in {pfs.cwd()}',logger.info)
        TC.write_top(f'{inpfnm}.top')
        cp.set(self.TopoCoord,stepname='initialize_topology')

    def initialize_coordinates(self,inpfnm='init'):
        """Builds initial top and gro files for initial liquid simulation

        :param inpfnm: input file name prefix, defaults to 'init'
        :type inpfnm: str, optional
        """
        if cp.passed('initialize_coordinates'): return
        TC=self.TopoCoord
        cwd=pfs.go_to('systems/init')
        densification_dict=self.cfg.parameters.get('densification',{})
        # logger.debug(f'{densification_dict}')
        if not densification_dict:
            densification_dict['aspect_ratio']=self.cfg.parameters.get('aspect_ratio',np.array([1.,1.,1.]))
            if 'initial_density' in self.cfg.parameters:
                densification_dict['initial_density']=self.cfg.parameters['initial_density']
            if 'initial_boxsize' in self.cfg.parameters:
                densification_dict['initial_boxsize']=self.cfg.parameters['initial_boxsize']
        dspec=['initial_density' in densification_dict,'initial_boxsize' in densification_dict]
        # logger.debug(f'{dspec} {any(dspec)} {not all(dspec)}')
        assert any(dspec),'Neither of "initial_boxsize" nor "initial_density" are specfied'
        assert not all(dspec),'Cannot specify both "initial_boxsize" and "initial_density"'
        if 'initial_boxsize' in densification_dict:
            boxsize=densification_dict['initial_boxsize']
        else:
            mass_kg=TC.total_mass(units='SI')
            V0_m3=mass_kg/densification_dict['initial_density']
            ar=densification_dict.get('aspect_ratio',np.array([1.,1.,1.]))
            assert ar[0]==1.,f'Error: parameter aspect_ratio must be a 3-element-list with first element 1'
            ar_yx=ar[1]*ar[2]
            L0_m=(V0_m3/ar_yx)**(1./3.)
            L0_nm=L0_m*1.e9
            boxsize=np.array([L0_nm,L0_nm,L0_nm])*ar
            logger.info(f'Initial density: {densification_dict["initial_density"]} kg/m^3')
            logger.info(f'Total mass: {mass_kg:.3e} kg')
            logger.info(f'Box aspect ratio: {" x ".join([str(x) for x in ar])}')
        logger.info(f'Initial box side lengths: {boxsize[0]:.3f} nm x {boxsize[1]:.3f} nm x {boxsize[2]:.3f} nm')

        clist=self.cfg.initial_composition
        c_togromacs={}
        for cc in clist:
            M=self.cfg.molecules[cc['molecule']]
            tc=cc['count']
            ''' assuming racemic mixture of any stereoisomers '''
            total_isomers=len(M.stereoisomers)+1
            count_per_isomer=tc//total_isomers
            leftovers=tc%total_isomers
            c_togromacs[M.name]=count_per_isomer
            if leftovers>0:
                c_togromacs[M.name]+=1
                leftovers-=1
            pfs.checkout(f'molecules/parameterized/{M.name}.gro',altpath=[pfs.subpath('molecules')])
            for isomer in M.stereoisomers:
                c_togromacs[isomer]=count_per_isomer
                if leftovers>0:
                    c_togromacs[isomer]+=1
                    leftovers-=1
                pfs.checkout(f'molecules/parameterized/{isomer}.gro',altpath=[pfs.subpath('molecules')])
        msg=insert_molecules(c_togromacs,boxsize,inpfnm,**self.cfg.parameters)
        TC.read_gro(f'{inpfnm}.gro')
        TC.atom_count()
        TC.set_grx_attributes(['z','nreactions','reactantName','cycle','cycle_idx','chain','chain_idx'])
        TC.inherit_grx_attributes_from_molecules(self.cfg.molecules,self.cfg.initial_composition,
            globally_unique=[False,False,False,True,False,True,False],
            unset_defaults=[0,0,'UNSET',-1,-1,-1,-1])
        for list_name in ['cycle','chain']:
            TC.reset_idx_list_from_grx_attributes(list_name)
        chainlists=TC.idx_lists['chain']
        # logger.debug(f'virgin chains')
        # for i,c in enumerate(chainlists):
        #     logger.debug(f'  {i} {c}')
        TC.make_resid_graph()
        TC.write_grx_attributes(f'{inpfnm}.grx')
        logger.info(f'Generated {inpfnm}.gro and {inpfnm}.grx in {pfs.cwd()}')
        cp.set(TC,stepname='initialize_coordinates')

    def do_densification(self,inpfnm='init',deffnm='densified'):
        """do_liquid_simulation Manages execution of gmx mdrun to perform minimization
            and NPT MD simulation of the initial liquid system.  Final coordinates are
            loaded into the global TopoCoord.

        :param inpfnm: input file name prefix, defaults to 'init'
        :type inpfnm: str, optional
        :param deffnm: deffnm prefix fed to gmx mdrun, defaults to 'npt-1'
        :type deffnm: str, optional
        """
        if cp.passed('do_densification'): return
        cwd=pfs.go_to('systems/init')
        gromacs_dict=self.cfg.parameters.get('gromacs',{})
        TC=self.TopoCoord
        infiles=[f'{inpfnm}.top',f'{inpfnm}.gro']
        outfiles=[f'{deffnm}.gro',f'{deffnm}.trr',f'{deffnm}.edr']
        assert all([os.path.exists(x) for x in infiles])
        densification_dict=self.cfg.parameters.get('densification',{})
        if not densification_dict:
            densification_dict['nsteps']=self.cfg.parameters.get('densification_steps',150000)
            densification_dict['temperature']=self.cfg.parameters.get('densification_temperature',300)
            densification_dict['pressure']=self.cfg.parameters.get('densification_pressure',10)
        logger.info(f'Densification for {densification_dict["nsteps"]} steps at {densification_dict["temperature"]} K and {densification_dict["pressure"]} bar')
        pfs.checkout(f'mdp/min.mdp')
        msg=TC.grompp_and_mdrun(out=f'{inpfnm}-minimized',mdp='min',quiet=False,**gromacs_dict)
        pfs.checkout(f'mdp/liquid-densify-npt.mdp')
        mod_dict={'ref_t':densification_dict['temperature'],'gen-temp':densification_dict['temperature'],'gen-vel':'yes','ref_p':densification_dict['pressure'],'nsteps':densification_dict['nsteps']}
        mdp_modify(f'liquid-densify-npt.mdp',mod_dict)
        msg=TC.grompp_and_mdrun(out=deffnm,mdp='liquid-densify-npt',quiet=False,**gromacs_dict)
        assert all([os.path.exists(x) for x in outfiles])
        logger.info(f'Densified coordinates in {pfs.cwd()}/{deffnm}.gro')
        gmx_energy_trace(deffnm,['Density'],report_averages=True,**gromacs_dict)
        trace('Density',[deffnm],outfile='../../plots/init-densification.png')
        # update coordinates; will wrap upon reading in
        # TC.copy_coords(TopoCoord(grofilename=f'{deffnm}.gro'))
        box=TC.Coordinates.box.diagonal()
        logger.info(f'Current box side lengths: {box[0]:.3f} nm x {box[1]:.3f} nm x {box[2]:.3f} nm')
        cp.set(TC,'do_densification')

    def do_precure_equilibration(self,deffnm='precure_equilibrated'):
        if cp.passed('do_precure_equilibration'): return
        pe_dict=self.cfg.parameters.get('precure_equilibration',{})
        gromacs_dict=self.cfg.parameters.get('gromacs',{})
        if not pe_dict: return # pre-cure equilibration is done only if the precure_equilibration dictionary is present
        cwd=pfs.go_to('systems/init')
        TC=self.TopoCoord
        nsteps=pe_dict.get('nsteps',50000)
        T=pe_dict.get('temperature',300)
        P=pe_dict.get('pressure',1)
        logger.info(f'Precure equilibration for {pe_dict["nsteps"]} steps at {pe_dict["temperature"]} K and {pe_dict["pressure"]} bar')
        mdp_pfx='equilibrate-npt'
        pfs.checkout(f'mdp/{mdp_pfx}.mdp')
        mod_dict={'ref_t':T,'gen-temp':T,'gen-vel':'yes','ref_p':P,'nsteps':nsteps}
        mdp_modify(f'{mdp_pfx}.mdp',mod_dict)
        msg=TC.grompp_and_mdrun(out=deffnm,mdp=mdp_pfx,quiet=False,**gromacs_dict)
        logger.info(f'Equilibrated coordinates in {deffnm}.gro')
        gmx_energy_trace(deffnm,['Density'],report_averages=True,**gromacs_dict)
        trace('Density',[deffnm],outfile='../../plots/init-equil-density.png')
        box=TC.Coordinates.box.diagonal()
        logger.info(f'Current box side lengths: {box[0]:.3f} nm x {box[1]:.3f} nm x {box[2]:.3f} nm')
        cp.set(TC,'do_precure_equilibration')

    def do_cure(self):
        if cp.passed('cure'): return
        cc=self.cc
        TC=self.TopoCoord
        RL=self.cfg.reactions
        MD=self.molecules
        if cp.is_currentstepname('cure'):
            cc.iter=cp.last_substep()
        else:
            cc.reset()
        cc.setup(max_nxlinkbonds=self.cfg.maxconv,desired_nxlinkbonds=int(self.cfg.maxconv*cc.dicts['cure']['desired_conversion']),max_search_radius=min(TC.Coordinates.box.diagonal()/2))
        cure_finished=cc.is_cured()
        if cure_finished: return
        my_logger('Connect-Update-Relax-Equilibrate (CURE) begins',logger.info)
        logger.info(f'Attempting to form {cc.desired_nxlinkbonds} bonds')
        while not cure_finished:
            my_logger(f'Iteration {cc.iter} begins',logger.info)
            reentry=pfs.go_to(f'systems/iter-{cc.iter}')
            if os.path.exists('cure_controller_state.yaml'):
                logger.debug(f'Reading new cure controller in {pfs.cwd()}')
                self.cc=CureController.from_yaml('cure_controller_state.yaml')
                cc=self.cc
                logger.info(f'Restarting at {cc.cum_nxlinkbonds} bonds')
            TC.grab_files() # copy files locally
            cc.do_bondsearch(TC,RL,MD,reentry=reentry)
            cc.do_preupdate_dragging(TC)
            cc.do_topology_update(TC,MD)
            cc.do_relax(TC)
            cc.do_equilibrate(TC)
            cp.subset(TC,'cure',cc.iter)
            logger.info(f'Iteration {cc.iter} current conversion {cc.curr_conversion():.3f} or {cc.cum_nxlinkbonds} bonds')
            cure_finished=cc.is_cured()
            if not cure_finished:
                cure_finished=cc.next_iter()
            # exit(-1)
        cwd=pfs.go_to(f'systems/postcure')
        my_logger(f'Postcure begins',logger.info)
        cc.do_postcure_bondsearch(TC,RL,MD)
        cc.do_topology_update(TC,MD)
        cc.do_relax(TC)
        cc.do_equilibrate(TC)
        my_logger('Connect-Update-Relax-Equilibrate (CURE) ends',logger.info)
        cp.set(TC,'cure')

    def do_postcure_anneal(self,deffnm='postcure_annealed'):
        if cp.passed('do_postcure_anneal'): return
        pca_dict=self.cfg.parameters.get('postcure_anneal',{})
        gromacs_dict=self.cfg.parameters.get('gromacs',{})
        if not pca_dict: return 
        cwd=pfs.go_to('systems/postcure')
        TC=self.TopoCoord
        mdp_pfx='equilibrate-nvt'
        pfs.checkout(f'mdp/{mdp_pfx}.mdp')
        timestep=float(mdp_get(f'{mdp_pfx}.mdp','dt'))
        ncycles=pca_dict.get('ncycles',0)
        cycle_segments=pca_dict.get('cycle_segments',[])
        temps=[str(r['T']) for r in cycle_segments]
        durations=[r['ps'] for r in cycle_segments]
        cycle_duration=sum(durations)
        total_duration=cycle_duration*ncycles
        nsteps=int(total_duration/timestep)
        cum_time=durations.copy()
        for i in range(1,len(cum_time)):
            cum_time[i]+=cum_time[i-1]
        my_logger(f'Postcure anneal for {nsteps} steps',logger.info)
        mod_dict={
            'ref_t':pca_dict.get('initial_temperature',300.0),
            'gen-temp':pca_dict.get('initial_temperature',300.0),
            'gen-vel':'yes',
            'annealing-npoints':len(cycle_segments),
            'annealing-temp':' '.join(temps),
            'annealing-time':' '.join([f'{x:.2f}' for x in cum_time]),
            'annealing':'periodic' if ncycles>1 else 'single',
            'nsteps':nsteps
            }
        mdp_modify(f'{mdp_pfx}.mdp',mod_dict)
        msg=TC.grompp_and_mdrun(out=deffnm,mdp=mdp_pfx,quiet=False,**gromacs_dict)
        my_logger(f'Annealed coordinates in {deffnm}.gro',logger.info)
        gmx_energy_trace(deffnm,['Density'],report_averages=True,**gromacs_dict)
        trace('Temperature',[deffnm],outfile='../../plots/postcure-anneal-temperature.png')
        cp.set(TC,'do_postcure_anneal')

    def do_postanneal_equilibration(self,deffnm='postannealed_equilibrated'):
        if cp.passed('do_postanneal_equilibration'): return
        pae_dict=self.cfg.parameters.get('postanneal_equilibration',{})
        gromacs_dict=self.cfg.parameters.get('gromacs',{})
        if not pae_dict: return
        cwd=pfs.go_to('systems/postcure')
        TC=self.TopoCoord
        nsteps=pae_dict.get('nsteps',50000)
        T=pae_dict.get('temperature',300)
        P=pae_dict.get('pressure',1)
        my_logger(f'Postanneal equilibration at {T} K and {P} bar for {nsteps} steps',logger.info)
        mdp_pfx='equilibrate-npt'
        pfs.checkout(f'mdp/{mdp_pfx}.mdp')
        mod_dict={'ref_t':T,'gen-temp':T,'gen-vel':'yes','ref_p':P,'nsteps':nsteps}
        mdp_modify(f'{mdp_pfx}.mdp',mod_dict)
        msg=TC.grompp_and_mdrun(out=deffnm,mdp=mdp_pfx,quiet=False,**gromacs_dict)
        logger.info(f'Equilibrated coordinates in {deffnm}.gro')
        gmx_energy_trace(deffnm,['Density'],report_averages=True,**gromacs_dict)
        trace('Density',[deffnm],outfile=f'../../plots/postanneal-equilibration-density.png')
        box=TC.Coordinates.box.diagonal()
        logger.info(f'Current box side lengths: {box[0]:.3f} nm x {box[1]:.3f} nm x {box[2]:.3f} nm')
        cp.set(TC,'do_postanneal_equilibration')

    def save_data(self,result_name='final'):
        TC=self.TopoCoord
        pfs.go_to(f'systems/{result_name}-results')
        logger.info(f'Saving final data to {pfs.cwd()}')
        TC.write_grx_attributes(f'{result_name}.grx')
        TC.write_gro(f'{result_name}.gro')
        TC.write_top(f'{result_name}.top')
        cp.set(TC,'final')

    def build(self,**kwargs):
        force_parameterization=kwargs.get('force_parameterization',False)
        force_checkin=kwargs.get('force_checkin',False)
        checkpoint_file=kwargs.get('checkpoint_file','checkpoint_state.yaml')
        TC=self.TopoCoord

        pfs.go_proj()

        self.generate_molecules(
            force_parameterization=force_parameterization,  # force antechamber/GAFF parameterization
            force_checkin=force_checkin                     # force check-in to system libraries
        )

        pfs.go_proj()
        cp.setup(TC,filename=checkpoint_file)
        TC.load_files()
        self.initialize_topology()
        self.initialize_coordinates()
        self.do_densification()
        self.do_precure_equilibration()
        self.do_cure()
        self.do_postcure_anneal()
        self.do_postanneal_equilibration()
        self.save_data()

