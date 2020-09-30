#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________
import numpy as np
import six
import abc
from pyomo.environ import Var, Constraint, value
from pyomo.core.base.block import _BlockData, declare_custom_block
from ..sparse.block_matrix import BlockMatrix
from scipy.sparse import coo_matrix
import pyomo.environ as pyo

"""This module is used for interfacing an external model (e.g., compiled
code) with a Pyomo model and then solve the composite model with
CyIpopt.

To use this interface:
   * inherit from ExternalGreyBoxModel and implement the necessary methods

   * create a CyIpoptCompositeExtProblemInterface object, giving it your
     pyomo model, an instance of the derived ExternalGreyBoxModel, a
     list of the Pyomo variables that map to the inputs of the external
     model, and a list of the Pyomo variables that map to any outputs
     from the external model.

   * The standard CyIpopt solver interface can be called using the 
     CyIpoptCompositeExtProblemInterface.

See the PyNumero tests for this interface to see an example of use.

Todo:

   * Currently, you cannot "fix" a pyomo variable that corresponds to an
     input or output and you must use a constraint instead (this is
     because Pyomo removes fixed variables before sending them to the
     solver)

   * Remove the dummy variable and constraint if possible

"""

@six.add_metaclass(abc.ABCMeta)
class ExternalGreyBoxModel(object):
    """
    This is the base class for building external input output models
    for use with Pyomo and CyIpopt
    """
    def __init__(self):
        pass

    def n_inputs(self):
        return len(self.input_names())

    def n_equality_constraints(self):
        return len(self.equality_constraint_names())

    def n_outputs(self):
        return len(self.output_names())

    @abc.abstractmethod
    def input_names(self):
        """
        Provide the list of string names to corresponding to the inputs
        of this external model. These should be returned in the order
        that they are to be used in set_input_values.
        """
        pass

    @abc.abstractmethod
    def equality_constraint_names(self):
        """
        Provide the list of string names corresponding to any residuals 
        for this external model. These should be in the order corresponding
        to values returned from evaluate_residuals. Return an empty list
        if there are no equality constraints.
        """
        return []

    @abc.abstractmethod
    def output_names(self):
        """
        Provide the list of string names corresponding to the outputs
        of this external model. These should be in the order corresponding
        to values returned from evaluate_outputs. Return an empty list if there
        are no computed outputs.
        """
        return []

    @abc.abstractmethod
    def set_input_values(self, input_values):
        """
        This method is called by the solver to set the current values
        for the input variables. The derived class must cache these if
        necessary for any subsequent calls to evalute_outputs or
        evaluate_derivatives.
        """
        pass

    def get_equality_constraint_scaling_factors(self):
        """
        This method is called by the solver interface to get desired
        values for scaling the equality constraints. None means no
        scaling is desired. Note that, depending on the solver,
        one may need to set solver options so these factors are used
        """
        return None
    
    def get_output_constraint_scaling_factors(self):
        """
        This method is called by the solver interface to get desired
        values for scaling the constraints with output variables. Returning
        None means that no scaling of the output constraints is desired.
        Note that, depending on the solver, one may need to set solver options
        so these factors are used
        """
        return None

    @abc.abstractmethod
    def evaluate_equality_constraints(self):
        """
        Compute the residuals from the model (using the values
        set in input_values) and return as a numpy array
        """
        pass

    @abc.abstractmethod
    def evaluate_outputs(self):
        """
        Compute the outputs from the model (using the values
        set in input_values) and return as a numpy array
        """
        pass

    @abc.abstractmethod
    def evaluate_jacobian_equality_constraints(self):
        """
        Compute the derivatives of the residuals with respect
        to the inputs (using the values set in input_values).
        This should be a scipy matrix with the rows in
        the order of the residual names and the cols in
        the order of the input variables.
        """
        pass

    @abc.abstractmethod
    def evaluate_jacobian_outputs(self):
        """
        Compute the derivatives of the outputs with respect
        to the inputs (using the values set in input_values).
        This should be a scipy matrix with the rows in
        the order of the output variables and the cols in
        the order of the input variables.
        """
        pass

    # ToDo: Hessians not yet handled


@declare_custom_block(name='ExternalGreyBoxBlock', new_ctype=True)
class ExternalGreyBoxBlockData(_BlockData):

    # TODO CHANGE THE CTYPE
    
    def set_external_model(self, external_grey_box_model):
        self._ex_model = ex_model = external_grey_box_model

        self._input_names = ex_model.input_names()
        if self._input_names is None or len(self._input_names) == 0:
            raise ValueError(
                'No input_names specified for external_grey_box_model.'
                ' Must specify at least one input.')
        self.inputs = pyo.Var(self._input_names)

        self._equality_constraint_names = ex_model.equality_constraint_names()
        self._output_names = ex_model.output_names()

        # Note, this works even if output_names is an empty list
        self.outputs = pyo.Var(self._output_names)

    def get_external_model(self):
        return self._ex_model

class _ExternalGreyBoxModelHelper(object):
    def __init__(self, ex_grey_box_block, vardata_to_idx, initial_primal_values):
        """This helper takes an ExternalGreyBoxModel and provides the residual
        and Jacobian computation.

        The ExternalGreyBoxModel provides an interface that supports
        equality constraints (pure residuals) and output equations. Let
        u be the inputs, o be the outputs, and x be the full set of
        primal variables from the entire pyomo_nlp.

        With this, the ExternalGreyBoxModel provides the residual
        computations w_eq(u), and w_o(u), as well as the Jacobians,
        Jw_eq(u), and Jw_o(u). This helper provides h(x)=0, where h(x) =
        [h_eq(x); h_o(x)-o] and h_eq(x)=w_eq(Pu*x), and
        h_o(x)=w_o(Pu*x), and Pu is a mapping from the full primal
        variables "x" to the inputs "u".

        It also provides the Jacobian of h w.r.t. x.
           J_h(x) = [Jw_eq(Pu*x); Jw_o(Pu*x)-Po*x]
        where Po is a mapping from the full primal variables "x" to the
        outputs "o".

        """
        self._block = ex_grey_box_block
        self._ex_model = ex_grey_box_block.get_external_model()
        self._n_primals = len(initial_primal_values)
        n_inputs = len(self._block.inputs)
        n_outputs = len(self._block.outputs)

        # store the map of input indices (0 .. n_inputs) to
        # the indices in the full primals vector
        self._inputs_to_primals_map = []
        for k in self._block.inputs:
            self._inputs_to_primals_map.append(vardata_to_idx[self._block.inputs[k]])

        # store the map of output indices (0 .. n_outputs) to
        # the indices in the full primals vector
        self._outputs_to_primals_map = []
        for k in self._block.outputs:
            self._outputs_to_primals_map.append(vardata_to_idx[self._block.outputs[k]])
        
        # setup some structures for the jacobians
        input_values = initial_primal_values[self._inputs_to_primals_map]
        self._ex_model.set_input_values(input_values)

        if self._ex_model.n_outputs() == 0 and \
           self._ex_model.n_equality_constraints() == 0:
            raise ValueError(
                'ExternalGreyBoxModel has no equality constraints '
                'or outputs. It must have at least one or both.')

        # we need to change the column indices in the jacobian
        # from the 0..n_inputs provided by the external model
        # to the indices corresponding to the full Pyomo model
        # so we create that here
        self._eq_jac_jcol_for_primals = None
        if self._ex_model.n_equality_constraints() > 0:
            jac = self._ex_model.evaluate_jacobian_equality_constraints()
            self._eq_jac_jcol_for_primals = np.asarray(
                [self._inputs_to_primals_map[j] for j in jac.col],
                dtype=np.int64)

        self._outputs_jac_jcol_for_primals = None
        if self._ex_model.n_outputs() > 0:
            jac = self._ex_model.evaluate_jacobian_outputs()
            self._outputs_jac_jcol_for_primals = np.asarray(
                [self._inputs_to_primals_map[j] for j in jac.col],
                dtype=np.int64)

        # create the irow, jcol, nnz structure for the
        # output variable portion of h(u)-o=0
        self._additional_output_entries_irow = \
            np.asarray([i for i in range(n_outputs)])
        self._additional_output_entries_jcol = \
            np.asarray([self._outputs_to_primals_map[j]
                        for j in range(n_outputs)])
        self._additional_output_entries_data = \
            -1.0*np.ones(n_outputs)

    def set_primals(self, primals):
        # map the full primals "x" to the inputs "u" and set
        # the values on the external model 
        input_values = primals[self._inputs_to_primals_map]
        self._ex_model.set_input_values(input_values)

        # map the full primals "x" to the outputs "o" and
        # store a vector of the current output values to
        # use when evaluating residuals
        self._output_values = primals[self._outputs_to_primals_map]

    def n_residuals(self):
        return self._ex_model.n_equality_constraints() \
            + self._ex_model.n_outputs()

    def get_residual_scaling(self):
        eq_scaling = self._ex_model.get_equality_constraint_scaling_factors()
        output_con_scaling = self._ex_model.get_output_constraint_scaling_factors()
        if eq_scaling is None and output_con_scaling is None:
            return None
        if eq_scaling is None:
            eq_scaling = np.ones(self._ex_model.n_equality_constraints())
        if output_con_scaling is None:
            output_con_scaling = np.ones(self._ex_model.n_outputs())

        return np.concatenate((
            eq_scaling,
            output_con_scaling))

    def evaluate_residuals(self):
        # evalute the equality constraints and the output equations
        # and return a single vector of residuals
        # returns residual for h(x)=0, where h(x) = [h_eq(x); h_o(x)-o]
        resid_list = []
        if self._ex_model.n_equality_constraints() > 0:
            resid_list.append(self._ex_model.evaluate_equality_constraints())

        if self._ex_model.n_outputs() > 0:
            computed_output_values = self._ex_model.evaluate_outputs()
            output_resid = computed_output_values - self._output_values
            resid_list.append(output_resid)
            
        return np.concatenate(resid_list)
        
    def evaluate_jacobian(self):
        # compute the jacobian of h(x) w.r.t. x
        # J_h(x) = [Jw_eq(Pu*x); Jw_o(Pu*x)-Po*x]

        # Jw_eq(x)
        eq_jac = None
        if self._ex_model.n_equality_constraints() > 0:
            eq_jac = self._ex_model.evaluate_jacobian_equality_constraints()
            # map the columns from the inputs "u" back to the full primals "x"
            eq_jac.col = self._eq_jac_jcol_for_primals
            eq_jac = coo_matrix((eq_jac.data, (eq_jac.row, eq_jac.col)), shape=(eq_jac.shape[0], self._n_primals))

        outputs_jac = None
        if self._ex_model.n_outputs() > 0:
            outputs_jac = self._ex_model.evaluate_jacobian_outputs()

            row = outputs_jac.row
            # map the columns from the inputs "u" back to the full primals "x"
            col = self._outputs_jac_jcol_for_primals
            data = outputs_jac.data

            # add the additional entries for the -Po*x portion of the jacobian
            row = np.concatenate((row, self._additional_output_entries_irow))
            col = np.concatenate((col, self._additional_output_entries_jcol))
            data  = np.concatenate((data, self._additional_output_entries_data))
            outputs_jac = coo_matrix((data, (row, col)), shape=(outputs_jac.shape[0], self._n_primals))

        jac = None
        if eq_jac is not None:
            if outputs_jac is not None:
                # create a jacobian with both Jw_eq and Jw_o
                jac = BlockMatrix(2,1)
                jac.name = 'external model jacobian'
                jac.set_block(0,0,eq_jac)
                jac.set_block(1,0,outputs_jac)
            else:
                assert self._ex_model.n_outputs() == 0
                assert self._ex_model.n_equality_constraints() > 0
                # only need the Jacobian with Jw_eq (there are not
                # output equations)
                jac = eq_jac
        else:
            assert outputs_jac is not None
            assert self._ex_model.n_outputs() > 0
            assert self._ex_model.n_equality_constraints() == 0
            # only need the Jacobian with Jw_o (there are no equalities)
            jac = outputs_jac

        return jac
