from typing import Literal
import numpy as np
from pathlib import Path

import torch
from torch import nn, linspace
import torch.nn.functional as F
from torch.distributions import Normal

import pytorch_lightning as pl

from scvi.distributions import NegativeBinomial
from celldreamer.models.base.cell_decoder import CellDecoder
from celldreamer.models.base.utils import MLP
from celldreamer.eval.evaluate import compute_umap_and_wasserstein
from celldreamer.models.base.utils import pad_t_like_x
from celldreamer.models.fm.ode import torch_wrapper
from celldreamer.models.fm.ot_sampler import OTPlanSampler

from torchdyn.core import NeuralODE

class FM(pl.LightningModule):
    def __init__(self,
                 denoising_model: nn.Module,
                 feature_embeddings: dict, 
                 x0_from_x_kwargs: dict,
                 plotting_folder: Path,
                 in_dim: int,
                 size_factor_statistics: dict,
                 scaler, 
                 conditioning_covariate: str, 
                 model_type: str,
                 encoder_type: str = "fixed", 
                 learning_rate: float = 0.001, 
                 weight_decay: float = 0.0001, 
                 antithetic_time_sampling: bool = True, 
                 scaling_method: str = "log_normalization",  # Change int to str
                 pretrain_encoder: bool = False,  # Change float to bool
                 pretraining_encoder_epochs: int = 0, 
                 sigma: float = 0.1, 
                 covariate_specific_theta: float = False, 
                 plot_and_eval_every=100):
        """
        Variational Diffusion Model (VDM).

        Args:
            denoising_model (nn.Module): Denoising model.
            feature_embeddings (dict): Feature embeddings for covariates.
            x0_from_x_kwargs (dict): Arguments for the x0_from_x MLP.
            plotting_folder (Path): Folder for saving plots.
            in_dim (int): Number of genes.
            conditioning_covariate (str): Covariate controlling the size factor sampling.
            learning_rate (float, optional): Learning rate. Defaults to 0.001.
            weight_decay (float, optional): Weight decay. Defaults to 0.0001.
            antithetic_time_sampling (bool, optional): Use antithetic time sampling. Defaults to True.
            scaling_method (str, optional): Scaling method for input data. Defaults to "log_normalization".
            pretrain_encoder (bool, optional): Pretrain the likelihood encoder.
            pretraining_encoder_epochs (int, optional): How many epochs used for the pretraining.
            sigma (float, optional): variance around straight path for flow matching objective.
        """
        super().__init__()
        
        self.denoising_model = denoising_model.to(self.device)
        self.feature_embeddings = feature_embeddings
        self.learning_rate = learning_rate
        self.weight_decay = weight_decay
        self.in_dim = in_dim
        self.size_factor_statistics = size_factor_statistics
        self.scaler = scaler
        self.encoder_type = encoder_type
        self.antithetic_time_sampling = antithetic_time_sampling
        self.scaling_method = scaling_method
        self.plotting_folder = plotting_folder
        self.pretrain_encoder = pretrain_encoder
        self.pretraining_encoder_epochs = pretraining_encoder_epochs
        self.model_type = model_type
        self.conditioning_covariate = conditioning_covariate
        self.sigma = sigma
        self.covariate_specific_theta = covariate_specific_theta
        self.plot_and_eval_every = plot_and_eval_every
        
        # MSE lost for the Flow Matching algorithm 
        self.criterion = torch.nn.MSELoss()
                
        # Used to collect test outputs
        self.testing_outputs = []  
        
        # If the encoder is fixed, we just need an inverting decoder. If learnt, the decoding is simply the softmax operation 
        if encoder_type == "learnt_encoder":
            x0_from_x_kwargs["dims"] = [self.in_dim, *x0_from_x_kwargs["dims"], self.in_dim]
            self.x0_from_x = MLP(**x0_from_x_kwargs)
        elif encoder_type == "learnt_autoencoder":
            x0_from_x_kwargs["dims"] = [self.in_dim, *x0_from_x_kwargs["dims"]]  # Encoder params
            self.x0_from_x =  MLP(**x0_from_x_kwargs)
            x0_from_x_kwargs["dims"] = x0_from_x_kwargs["dims"][::-1] # Decoder params
            self.x_from_x0 = MLP(**x0_from_x_kwargs)
        else:
            self.cell_decoder = CellDecoder(self.encoder_type)
        
        # Define the (log) inverse dispersion parameter (negative binomial)
        if not covariate_specific_theta:
            self.theta = torch.nn.Parameter(torch.randn(self.in_dim), requires_grad=True)
        else:
            n_cat = self.feature_embeddings[conditioning_covariate].n_cat
            self.theta = torch.nn.Parameter(n_cat, torch.randn(self.in_dim), requires_grad=True)
        
        # save hyper-parameters to self.hparams (auto-logged by W&B)
        self.save_hyperparameters()
        
        # OT sampler
        self.ot_sampler = OTPlanSampler(method="exact")
    
    def training_step(self, batch, batch_idx):
        """
        Training step for VDM.

        Args:
            batch: Batch data.
            batch_idx: Batch index.

        Returns:
            torch.Tensor: Loss value.
        """
        return self._step(batch, dataset='train')

    def _step(self, batch, dataset: Literal['train', 'valid']):
        """
        Common step for training and validation.

        Args:
            batch: Batch data.
            dataset (Literal['train', 'valid']): Dataset type.

        Returns:
            torch.Tensor: Loss value.
        """
        # Collect observation
        x = batch["X"].to(self.device)
        
        # Collect labels 
        y = batch["y"]
        y_fea = self.feature_embeddings[self.conditioning_covariate](y[self.conditioning_covariate])

        # Scale batch to reasonable range 
        x_scaled = self.scaler.scale(batch["X_norm"].to(self.device), reverse=False)
        if self.encoder_type in ["learnt_encoder", "learnt_autoencoder"]:
            x0 = self.x0_from_x(x_scaled)
        else:
            x0 = x_scaled

        # Quantify size factor 
        size_factor = x.sum(1).unsqueeze(1)
        log_size_factor = torch.log(size_factor)
        
        ## Change the function cause you are not training a time embedding anymore 
        # Compute log p(x | x_0) to train theta
        if self.current_epoch < self.pretraining_encoder_epochs and self.pretrain_encoder:
            recons_loss_enc = self.log_probs_x_z0(x, x0, y[self.conditioning_covariate], size_factor)  
            recons_loss_enc = recons_loss_enc.sum(1)
            self.log(f"{dataset}/recons_loss_enc", recons_loss_enc.mean())
            
        # Freeze the encoder if the pretraining phase is done 
        if (self.current_epoch == self.pretraining_encoder_epochs and self.pretrain_encoder and self.encoder_type in ["learnt_encoder", "learnt_autoencoder"]):
            for param in self.x0_from_x.parameters():
                param.requires_grad = False
            if self.encoder_type=="learnt_autoencoder":
                for param in self.x_from_x0.parameters():
                    param.requires_grad = False
            # Reinstate the optimizer and weight_decay to selected values
            self.optimizers().param_groups[0]['lr'] = self.learning_rate
            self.optimizers().param_groups[0]['weight_decay'] = self.weight_decay
        
        if (self.current_epoch >= self.pretraining_encoder_epochs and self.pretrain_encoder) or not self.pretrain_encoder:
            # Sample time 
            t = self._sample_times(x0.shape[0])  # B
            
            # Sample noise 
            z = self.sample_noise_like(x0)  # B x G
            
            # Get objective and 
            t, x_t, u_t = self.sample_location_and_conditional_flow(z, x0, t)

            # Forward through the model 
            v_t = self.denoising_model(x_t, t, log_size_factor, y_fea)
            loss = self.criterion(u_t, v_t)  # (B, )
            
            # Save results
            metrics = {
                "batch_size": x.shape[0],
                f"{dataset}/fm_loss": loss.mean()}
            self.log_dict(metrics, prog_bar=True)
         
        else:
            loss = recons_loss_enc
        
        # Log the final loss
        self.log(f"{dataset}/loss", loss.mean(), prog_bar=True)
        return loss.mean()
    
    # Private methods
    def _featurize_batch_y(self, batch):
        """
        Featurize all the covariates 

        Args:
            batch: Batch data.

        Returns:
            torch.Tensor: Featurized covariates.
        """
        y = []     
        for feature_cat in batch["y"]:
            y_cat = self.feature_embeddings[feature_cat](batch["y"][feature_cat])
            y.append(y_cat)
        y = torch.cat(y, dim=1).to(self.device)
        return y
    
    def _sample_times(self, batch_size):
        """
        Sample times, can be sampled to cover the 

        Args:
            batch_size (int): Batch size.

        Returns:
            torch.Tensor: Sampled times.
        """
        if self.antithetic_time_sampling:
            t0 = np.random.uniform(0, 1 / batch_size)
            times = torch.arange(t0, 1.0, 1.0 / batch_size, device=self.device)
        else:
            times = torch.rand(batch_size, device=self.device)
        return times

    @torch.no_grad()
    def sample(self, batch_size, n_sample_steps, covariate, covariate_indices=None, log_size_factor=None):
        z = torch.randn((batch_size, self.denoising_model.in_dim), device=self.device)

        # Sample random classes from the sampling covariate 
        if covariate_indices==None:
            covariate_indices = torch.randint(0, self.feature_embeddings[covariate].n_cat, 
                                        (batch_size,))
        if log_size_factor==None:
            # If size factor conditions the denoising, sample from the log-norm distribution. Else the size factor is None
            mean_size_factor, sd_size_factor = self.size_factor_statistics["mean"][covariate], self.size_factor_statistics["sd"][covariate]
            mean_size_factor, sd_size_factor = mean_size_factor[covariate_indices], sd_size_factor[covariate_indices]
            size_factor_dist = Normal(loc=mean_size_factor, scale=sd_size_factor)
            log_size_factor = size_factor_dist.sample().to(self.device).view(-1, 1)
            
        y = self.feature_embeddings[covariate](covariate_indices.cuda())

        t = linspace(0.0, 1.0, n_sample_steps, device=self.device)
        
        denoising_model_ode = torch_wrapper(self.denoising_model, log_size_factor, y)    
        
        self.node = NeuralODE(denoising_model_ode,
                                solver="dopri5", 
                                sensitivity="adjoint", 
                                atol=1e-4, 
                                rtol=1e-4)        
        
        x0 = self.node.trajectory(z, t_span=t)[-1]
        
        size_factor = torch.exp(log_size_factor)
        # Decode to parameterize negative binomial
        x = self._decode(x0, size_factor)
        del x0
        
        if not self.covariate_specific_theta:
            distr = NegativeBinomial(mu=x, theta=torch.exp(self.theta))
        else:
            distr = NegativeBinomial(mu=x, theta=torch.exp(self.theta[covariate_indices]))

        sample = distr.sample()
        return sample

    @torch.no_grad()
    def batched_sample(self, batch_size, repetitions, n_sample_steps, covariate, covariate_indices=None, log_size_factor=None):
        total_samples = []
        for i in range(repetitions):
            covariate_indices_batch = covariate_indices[(i*batch_size):((i+1)*batch_size)] if covariate_indices != None else None
            log_size_factor_batch = log_size_factor[(i*batch_size):((i+1)*batch_size)] if log_size_factor != None else None
            X_samples = self.sample(batch_size, n_sample_steps, covariate, covariate_indices_batch, log_size_factor_batch)
            total_samples.append(X_samples.cpu())
        return torch.cat(total_samples, dim=0)

    def log_probs_x_z0(self, x, x_0, y, size_factor):
        """
        Compute log p(x | z_0) for all possible values of each pixel in x.

        Args:
            x: Input data.
            z_0: Latent variable.
            size_factor (float): size factor.

        Returns:
            torch.Tensor: Log probabilities.
        """
        x_hat = self._decode(x_0, size_factor)
        
        if not self.covariate_specific_theta:
            distr = NegativeBinomial(mu=x_hat, theta=torch.exp(self.theta))
        else:
            distr = NegativeBinomial(mu=x_hat, theta=torch.exp(self.theta[y]))

        recon_loss = - distr.log_prob(x)
        return recon_loss

    def _decode(self, z, size_factor):
        # Decode the rescaled z
        if self.encoder_type not in ["learnt_autoencoder", "learnt_encoder"]:
            z = self.cell_decoder(self.scaler.scale(z, reverse=True), size_factor)
        else:
            if self.encoder_type=="learnt_autoencoder":
                z = self.x_from_x0(z)
            z = F.softmax(z, dim=1) * size_factor
        return z
    
    def sample_noise_like(self, x):
        return torch.randn_like(x)

    def sample_location_and_conditional_flow(self, x0, x1, t=None):
        """
        Compute the sample xt (drawn from N(t * x1 + (1 - t) * x0, sigma))
        and the conditional vector field ut(x1|x0) = x1 - x0, see Eq.(15) [1]
        with respect to the minibatch OT plan $\Pi$.

        Parameters
        ----------
        x0 : Tensor, shape (bs, *dim)
            represents the source minibatch
        x1 : Tensor, shape (bs, *dim)
            represents the target minibatch
        (optionally) t : Tensor, shape (bs)
            represents the time levels
            if None, drawn from uniform [0,1]

        Returns
        -------
        t : FloatTensor, shape (bs)
        xt : Tensor, shape (bs, *dim)
            represents the samples drawn from probability path pt
        ut : conditional vector field ut(x1|x0) = x1 - x0
        (optionally) epsilon : Tensor, shape (bs, *dim) such that xt = mu_t + sigma_t * epsilon

        References
        ----------
        [1] Improving and Generalizing Flow-Based Generative Models with minibatch optimal transport, Preprint, Tong et al.
        """
        # Resample from OT coupling 
        x0, x1 = self.ot_sampler.sample_plan(x0, x1)
        # Sample time 
        if t is None:
            t = torch.rand(x0.shape[0]).type_as(x0)
        assert len(t) == x0.shape[0], "t has to have batch size dimension"

        # Sample noise along straight line
        eps = self.sample_noise_like(x0)
        xt = self.sample_xt(x0, x1, t, eps)
        ut = self.compute_conditional_flow(x0, x1, t, xt)
        return t, xt, ut

    def sample_xt(self, x0, x1, t, epsilon):
        """
        Draw a sample from the probability path N(t * x1 + (1 - t) * x0, sigma), see (Eq.14) [1].

        Parameters
        ----------
        x0 : Tensor, shape (bs, *dim)
            represents the source minibatch
        x1 : Tensor, shape (bs, *dim)
            represents the target minibatch
        t : FloatTensor, shape (bs)
        epsilon : Tensor, shape (bs, *dim)
            noise sample from N(0, 1)

        Returns
        -------
        xt : Tensor, shape (bs, *dim)

        References
        ----------
        [1] Improving and Generalizing Flow-Based Generative Models with minibatch optimal transport, Preprint, Tong et al.
        """
        mu_t = self.compute_mu_t(x0, x1, t)
        sigma_t = self.compute_sigma_t(t)
        sigma_t = pad_t_like_x(sigma_t, x0)
        return mu_t + sigma_t * epsilon

    def compute_mu_t(self, x0, x1, t):
        """
        Compute the mean of the probability path N(t * x1 + (1 - t) * x0, sigma), see (Eq.14) [1].

        Parameters
        ----------
        x0 : Tensor, shape (bs, *dim)
            represents the source minibatch
        x1 : Tensor, shape (bs, *dim)
            represents the target minibatch
        t : FloatTensor, shape (bs)

        Returns
        -------
        mean mu_t: t * x1 + (1 - t) * x0

        References
        ----------
        [1] Improving and Generalizing Flow-Based Generative Models with minibatch optimal transport, Preprint, Tong et al.
        """
        t = pad_t_like_x(t, x0)
        mu_t = t * x1 + (1 - t) * x0
        return mu_t
    
    def compute_sigma_t(self, t):
        """
        Compute the standard deviation of the probability path N(t * x1 + (1 - t) * x0, sigma), see (Eq.14) [1].

        Parameters
        ----------
        t : FloatTensor, shape (bs)

        Returns
        -------
        standard deviation sigma

        References
        ----------
        [1] Improving and Generalizing Flow-Based Generative Models with minibatch optimal transport, Preprint, Tong et al.
        """
        del t
        return self.sigma
    
    def compute_conditional_flow(self, x0, x1, t, xt):
        """
        Compute the conditional vector field ut(x1|x0) = x1 - x0, see Eq.(15) [1].

        Parameters
        ----------
        x0 : Tensor, shape (bs, *dim)
            represents the source minibatch
        x1 : Tensor, shape (bs, *dim)
            represents the target minibatch
        t : FloatTensor, shape (bs)
        xt : Tensor, shape (bs, *dim)
            represents the samples drawn from probability path pt

        Returns
        -------
        ut : conditional vector field ut(x1|x0) = x1 - x0

        References
        ----------
        [1] Improving and Generalizing Flow-Based Generative Models with minibatch optimal transport, Preprint, Tong et al.
        """
        del t, xt
        return x1 - x0
    
    def compute_lambda(self, t):
        """Compute the lambda function, see Eq.(23) [3].

        Parameters
        ----------
        t : FloatTensor, shape (bs)

        Returns
        -------
        lambda : score weighting function

        References
        ----------
        [4] Simulation-free Schrodinger bridges via score and flow matching, Preprint, Tong et al.
        """
        sigma_t = self.compute_sigma_t(t)
        return 2 * sigma_t / (self.sigma**2 + 1e-8)

    def configure_optimizers(self):
        """
        Optimizer configuration 

        Returns:
            dict: Optimizer configuration.
        """
        params = list(self.parameters())
        
        if not self.feature_embeddings[self.conditioning_covariate].one_hot_encode_features:
            for cov in self.feature_embeddings:
                params += list(self.feature_embeddings[cov].parameters())
        
        if self.encoder_type in ["learnt_encoder", "learnt_autoencoder"]:
            optimizer = torch.optim.Adam(params, 
                                            lr=0.001)
        
        else:
            optimizer = torch.optim.Adam(params, 
                                        self.learning_rate, 
                                        weight_decay=self.weight_decay)

        return optimizer

    def validation_step(self, batch, batch_idx):
        """
        Validation step for VDM.

        Args:
            batch: Batch data.
            batch_idx: Batch index.

        Returns:
            torch.Tensor: Loss value.
        """
        return self._step(batch, dataset='valid')
    
    def test_step(self, batch, batch_idx):
        """
        Training step for VDM.

        Args:
            batch: Batch data.
            batch_idx: Batch index.

        Returns:
            torch.Tensor: Loss value.
        """
        self.testing_outputs.append(batch["X"].cpu())

    def on_test_epoch_end(self, *arg, **kwargs):
        self.compute_metrics_and_plots(dataset_type="test")
        self.testing_outputs = []

    @torch.no_grad()
    def compute_metrics_and_plots(self, dataset_type, *arg, **kwargs):
        """
        Concatenates all observations from the test data loader in a single dataset.

        Args:
            outputs: List of outputs from the test step.

        Returns:
            None
        """
        # Concatenate all test observations
        testing_outputs = torch.cat(self.testing_outputs, dim=0)
        
        # Plot UMAP of generated cells and real test cells
        wd = compute_umap_and_wasserstein(model=self, 
                                            batch_size=1000, 
                                            n_sample_steps=100, 
                                            plotting_folder=self.plotting_folder, 
                                            X_real=testing_outputs, 
                                            epoch=self.current_epoch,
                                            conditioning_covariate=self.conditioning_covariate)
        del testing_outputs
        metric_dict = {}
        for key in wd:
            metric_dict[f"{dataset_type}_{key}"] = wd[key]

        # Compute Wasserstein distance between real test set and generated data 
        self.log_dict(wd)
        return wd
    