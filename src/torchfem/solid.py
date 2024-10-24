import torch

from .elements import Hexa1, Hexa2, Tetra1, Tetra2
from .sparse import sparse_solve


class Solid:
    def __init__(self, nodes: torch.Tensor, elements: torch.Tensor, material):
        self.nodes = nodes
        self.n_dofs = torch.numel(self.nodes)
        self.elements = elements
        self.n_elem = len(self.elements)
        self.forces = torch.zeros_like(nodes)
        self.displacements = torch.zeros_like(nodes)
        self.constraints = torch.zeros_like(nodes, dtype=bool)
        if len(elements[0]) == 4:
            self.etype = Tetra1()
        elif len(elements[0]) == 8:
            self.etype = Hexa1()
        elif len(elements[0]) == 10:
            self.etype = Tetra2()
        elif len(elements[0]) == 20:
            self.etype = Hexa2()

        # Stack stiffness tensor (for general anisotropy and multi-material assignment)
        self.material = material.vectorize(self.n_elem)

        # Compute mapping from local to global indices (hard to read, but fast)
        N = self.n_elem
        idx = ((3 * self.elements).unsqueeze(-1) + torch.arange(3)).reshape(N, -1)
        idx1 = idx.unsqueeze(1).expand(N, idx.shape[1], -1)
        idx2 = idx.unsqueeze(-1).expand(N, -1, idx.shape[1])
        self.indices = torch.stack([idx1, idx2], dim=0)

    def J(self, q, nodes):
        """Jacobian and Jacobian determinant."""
        J = self.etype.B(q) @ nodes
        detJ = torch.linalg.det(J)
        if torch.any(detJ <= 0.0):
            raise Exception("Negative Jacobian. Check element numbering.")
        return J, detJ

    def D(self, B):
        """Element gradient operator"""
        zeros = torch.zeros(self.n_elem, self.etype.nodes)
        shape = [self.n_elem, -1]
        D0 = torch.stack([B[:, 0, :], zeros, zeros], dim=-1).reshape(shape)
        D1 = torch.stack([zeros, B[:, 1, :], zeros], dim=-1).reshape(shape)
        D2 = torch.stack([zeros, zeros, B[:, 2, :]], dim=-1).reshape(shape)
        D3 = torch.stack([zeros, B[:, 2, :], B[:, 1, :]], dim=-1).reshape(shape)
        D4 = torch.stack([B[:, 2, :], zeros, B[:, 0, :]], dim=-1).reshape(shape)
        D5 = torch.stack([B[:, 1, :], B[:, 0, :], zeros], dim=-1).reshape(shape)
        return torch.stack([D0, D1, D2, D3, D4, D5], dim=1)

    def integrate_step(self, de, ds, du):
        """Perform numerical integrations for element stiffness matrix."""
        nodes = self.nodes[self.elements, :]
        du = du[self.elements, :].reshape(self.n_elem, -1)
        k = torch.zeros((self.n_elem, 3 * self.etype.nodes, 3 * self.etype.nodes))
        f = torch.zeros((self.n_elem, 3 * self.etype.nodes))
        epsilon = torch.zeros(self.n_elem, 6)
        sigma = torch.zeros(self.n_elem, 6)
        for w, q in zip(self.etype.iweights(), self.etype.ipoints()):
            # Compute gradient operators
            J, detJ = self.J(q, nodes)
            B = torch.matmul(torch.linalg.inv(J), self.etype.B(q))
            D = self.D(B)
            # Evaluate material response
            dde = torch.einsum("...ij,...j->...i", D, du)
            e_new, s_new, ddsdde = self.material.step(dde, de, ds)
            # Compute contribution to element strain and stress
            epsilon[:, :] += (w * detJ)[:, None] * e_new
            sigma[:, :] += (w * detJ)[:, None] * s_new
            # Compute element internal forces
            f[:, :] += (w * detJ)[:, None] * torch.einsum("...ij,...i->...j", D, s_new)
            # Compute element stiffness matrix
            CD = torch.matmul(ddsdde, D)
            DCD = torch.matmul(CD.transpose(1, 2), D)
            k[:, :, :] += (w * detJ)[:, None, None] * DCD
        return k, f, epsilon, sigma

    def assemble_stiffness(self, k, con):
        """Assemble global stiffness matrix."""
        size = (self.n_dofs, self.n_dofs)
        # Unravel indices and values
        indices = self.indices.reshape((2, -1))
        values = k.ravel()
        # Eliminate and replace constrained dofs
        mask = ~(torch.isin(indices[0, :], con) | torch.isin(indices[1, :], con))
        diag_index = torch.stack((con, con), dim=0)
        diag_value = torch.ones_like(con, dtype=k.dtype)
        # Concatenate
        indices = torch.cat((indices[:, mask], diag_index), dim=1)
        values = torch.cat((values[mask], diag_value), dim=0)
        return torch.sparse_coo_tensor(indices, values, size=size).coalesce()

    def assemble_force(self, f):
        """Assemble global force vector."""
        F = torch.zeros((self.n_dofs))
        indices = self.indices[0, :, 0, :].ravel()
        values = f.ravel()
        return F.index_add_(0, indices, values)

    def solve(self, increments=[0, 1], max_iter=3, tol=1e-10):
        """Solve with Newton-Raphson method."""

        # Indexes of constrained and unconstrained degrees of freedom
        con = torch.nonzero(self.constraints.ravel(), as_tuple=False).ravel()

        epsilon = torch.zeros(self.n_elem, 6)
        sigma = torch.zeros(self.n_elem, 6)
        f = torch.zeros_like(self.nodes)
        u = torch.zeros_like(self.nodes)
        du = torch.zeros_like(self.nodes).ravel()

        for i in range(1, len(increments)):
            # Increment size
            inc = increments[i] - increments[i - 1]
            # Load increment
            F_ext = inc * self.forces.ravel()
            DU = inc * self.displacements.clone().ravel()
            for j in range(max_iter):
                du[con] = DU[con]
                # Element-wise integration
                k, f_int, epsilon_new, sigma_new = self.integrate_step(
                    epsilon, sigma, du.reshape((-1, 3))
                )

                # Assemble global stiffness matrix and internal force vector
                K = self.assemble_stiffness(k, con)
                F_int = self.assemble_force(f_int)

                # Compute residual
                residual = F_int - F_ext
                residual[con] = 0.0
                res_norm = torch.linalg.norm(residual)
                print(f"Residual (Increment {i}, Iteration {j}): {res_norm}")

                # Solve for displacement increment
                du -= sparse_solve(K, residual)

            # Update increment
            epsilon = epsilon_new
            sigma = sigma_new
            f = F_int.reshape((-1, 3))
            u += du.reshape((-1, 3))

        return u, f, sigma, epsilon

    @torch.no_grad()
    def plot(
        self,
        u=0.0,
        node_property=None,
        element_property=None,
        show_edges=True,
        show_undeformed=False,
        contour=None,
        cmap="viridis",
    ):
        try:
            import pyvista
        except ImportError:
            raise Exception("Plotting 3D requires pyvista.")

        pyvista.set_plot_theme("document")
        pl = pyvista.Plotter()
        pl.enable_anti_aliasing("ssaa")

        # VTK cell types
        if isinstance(self.etype, Tetra1):
            cell_types = self.n_elem * [pyvista.CellType.TETRA]
        elif isinstance(self.etype, Tetra2):
            cell_types = self.n_elem * [pyvista.CellType.QUADRATIC_TETRA]
        elif isinstance(self.etype, Hexa1):
            cell_types = self.n_elem * [pyvista.CellType.HEXAHEDRON]
        elif isinstance(self.etype, Hexa2):
            cell_types = self.n_elem * [pyvista.CellType.QUADRATIC_HEXAHEDRON]

        # VTK element list
        elements = []
        for element in self.elements:
            elements += [len(element), *element]

        # Deformed node positions
        pos = self.nodes + u

        # Create unstructured mesh
        mesh = pyvista.UnstructuredGrid(elements, cell_types, pos.tolist())

        # Plot node properties
        if node_property:
            for key, val in node_property.items():
                mesh.point_data[key] = val

        # Plot cell properties
        if element_property:
            for key, val in element_property.items():
                mesh.cell_data[key] = val

        if contour:
            mesh = mesh.cell_data_to_point_data()
            mesh = mesh.contour(contour)

        if show_edges:
            if isinstance(self.etype, Tetra2) or isinstance(self.etype, Hexa2):
                # Trick to plot edges for quadratic elements
                # See: https://github.com/pyvista/pyvista/discussions/5777
                surface = mesh.separate_cells().extract_surface(nonlinear_subdivision=4)
                edges = surface.extract_feature_edges()
                pl.add_mesh(surface, cmap=cmap)
                actor = pl.add_mesh(edges, style="wireframe", color="black")
                actor.mapper.SetResolveCoincidentTopologyToPolygonOffset()
            else:
                pl.add_mesh(mesh, cmap=cmap, show_edges=True)
        else:
            pl.add_mesh(mesh, cmap=cmap)

        if show_undeformed:
            undefo = pyvista.UnstructuredGrid(elements, cell_types, self.nodes.tolist())
            edges = (
                undefo.separate_cells()
                .extract_surface(nonlinear_subdivision=4)
                .extract_feature_edges()
            )
            pl.add_mesh(edges, style="wireframe", color="grey")

        pl.show(jupyter_backend="client")
