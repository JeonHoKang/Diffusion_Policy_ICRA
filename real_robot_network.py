#@markdown ### **Imports**
# diffusion policy import
from typing import Tuple, Sequence, Dict, Union, Optional, Callable
import numpy as np
import math
import torch
import torch.nn as nn
from diffusers.schedulers.scheduling_ddpm import DDPMScheduler
import os
from data_util import RealRobotDataSet_SingleView, RealRobotDataSet
from train_utils import train_utils
import json
#@markdown ### **Network**
#@markdown
#@markdown Defines a 1D UNet architecture `ConditionalUnet1D`
#@markdown as the noies prediction network
#@markdown
#@markdown Components
#@markdown - `SinusoidalPosEmb` Positional encoding for the diffusion iteration k
#@markdown - `Downsample1d` Strided convolution to reduce temporal resolution
#@markdown - `Upsample1d` Transposed convolution to increase temporal resolution
#@markdown - `Conv1dBlock` Conv1d --> GroupNorm --> Mish
#@markdown - `ConditionalResidualBlock1D` Takes two inputs `x` and `cond`. \
#@markdown `x` is passed through 2 `Conv1dBlock` stacked together with residual connection.
#@markdown `cond` is applied to `x` with [FiLM](https://arxiv.org/abs/1709.07871) conditioning.



import torch
import torch.nn as nn

class ForceEncoder(nn.Module):
    def __init__(self, force_dim, hidden_dim, batch_size, obs_horizon, cross_attn = False):
        super(ForceEncoder, self).__init__()
        self.cross_attn = cross_attn
        self.batch_size = batch_size
        self.obs_horizon = obs_horizon
        # Force feature extraction with Group Normalization
        # Convolutional layers to encode force data with Group Normalization
        self.conv_encoder = nn.Sequential(
            nn.Conv1d(in_channels=1, out_channels=32, kernel_size=3, stride=1, padding=1),
            nn.GroupNorm(num_groups=16, num_channels=32),
            nn.ReLU(),
            nn.Conv1d(in_channels=32, out_channels=64, kernel_size=3, stride=1, padding=1),
            nn.GroupNorm(num_groups=16, num_channels=64),
            nn.ReLU(),
            nn.Flatten()
        )


        self.projection_layer = nn.Linear(64 * force_dim, hidden_dim)

    def forward(self, x):
        x = x.unsqueeze(1)  # Reshape to [batch_size, 1, input_dim] => [64, 1, 4]
        latent_vector = self.conv_encoder(x)
        latent_vector = self.projection_layer(latent_vector)  # Shape: [batch_size, 512]
        if self.cross_attn:
            latent_vector = latent_vector.view(self.batch_size, 2, 512)
        return latent_vector
    
class CrossAttentionFusion(nn.Module):
    def __init__(self, image_dim, force_dim, hidden_dim:int = 512, batch_size = 48, obs_horizon = 2, resnet = True):
        super(CrossAttentionFusion, self).__init__()
        C,H,W = image_dim
        # Image feature extraction
        # Image feature extraction layers
        if not resnet:
            self.image_encoder = nn.Sequential(
                nn.Conv2d(in_channels=C, out_channels=64, kernel_size=3, stride=1, padding=1),
                nn.GroupNorm(num_groups=16, num_channels=64),  # Applying GroupNorm instead of BatchNorm
                nn.ReLU(),
                nn.MaxPool2d(kernel_size=2),
                nn.Conv2d(in_channels=64, out_channels=128, kernel_size=3, stride=1, padding=1),
                nn.GroupNorm(num_groups=16, num_channels=128),  # Applying GroupNorm to the second layer
                nn.ReLU(),
                nn.MaxPool2d(kernel_size=2),
                nn.Flatten()
            )
        else:
            self.image_encoder = train_utils().get_resnet("resnet18")
            self.image_encoder = train_utils().replace_bn_with_gn(self.image_encoder)

        # Dynamically calculate the image_dim after convolution and pooling
        with torch.no_grad():
            sample_input = torch.zeros(1, C, H, W)  # Batch size of 1
            sample_output = self.image_encoder(sample_input)
            image_dim = sample_output.shape[1]  # Get the flattened image dimension

        # Fully connected layer to map the image features to hidden_dim
        self.image_fc = nn.Linear(image_dim, hidden_dim)

        # Force feature extraction
        self.force_encoder = ForceEncoder(force_dim=force_dim, hidden_dim=hidden_dim, batch_size = batch_size, obs_horizon = obs_horizon, cross_attn=True)
        # Cross-attention layers
        self.attention = nn.MultiheadAttention(embed_dim=hidden_dim, num_heads=4)

        # Fusion layers to create joint embedding
        self.fusion_layer = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim)
        )

    def forward(self, image_input, force_input, batch_size, obs_horizon):
        # Encode image and force data
        image_features = self.image_encoder(image_input)

        image_features = self.image_fc(image_features)
        image_features = image_features.view(batch_size, obs_horizon, -1)

        image_features = image_features.permute(1, 0, 2)  # Correct shape: (num_images, batch_size, hidden_dim)

        # Reshape for attention: (sequence_length, batch_size, hidden_dim)

        force_features = self.force_encoder(force_input)

        # force_features = force_features.view(batch_size, obs_horizon, -1)
        force_features = force_features.permute(1, 0, 2)  # Correct shape: (num_forces, batch_size, hidden_dim)


        # Cross-attention operation
        attn_output, _ = self.attention(query=force_features, key=image_features, value=image_features)
        attn_output = attn_output.permute(1, 0, 2)  # Shape: (batch_size, num_forces, hidden_dim)

        # Generate the fused embedding
        joint_embedding = self.fusion_layer(attn_output)

        return joint_embedding

class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NumpyEncoder, self).default(obj)
    
class SinusoidalPosEmb(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.dim = dim

    def forward(self, x):
        device = x.device
        half_dim = self.dim // 2
        emb = math.log(10000) / (half_dim - 1)
        emb = torch.exp(torch.arange(half_dim, device=device) * -emb)
        emb = x[:, None] * emb[None, :]
        emb = torch.cat((emb.sin(), emb.cos()), dim=-1)
        return emb


class Downsample1d(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.conv = nn.Conv1d(dim, dim, 3, 2, 1)

    def forward(self, x):
        return self.conv(x)

class Upsample1d(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.conv = nn.ConvTranspose1d(dim, dim, 4, 2, 1)

    def forward(self, x):
        return self.conv(x)


class Conv1dBlock(nn.Module):
    '''
        Conv1d --> GroupNorm --> Mish
    '''

    def __init__(self, inp_channels, out_channels, kernel_size, n_groups=8):
        super().__init__()

        self.block = nn.Sequential(
            nn.Conv1d(inp_channels, out_channels, kernel_size, padding=kernel_size // 2),
            nn.GroupNorm(n_groups, out_channels),
            nn.Mish(),
        )

    def forward(self, x):
        return self.block(x)


class ConditionalResidualBlock1D(nn.Module):
    def __init__(self,
            in_channels,
            out_channels,
            cond_dim,
            kernel_size=3,
            n_groups=8):
        super().__init__()

        self.blocks = nn.ModuleList([
            Conv1dBlock(in_channels, out_channels, kernel_size, n_groups=n_groups),
            Conv1dBlock(out_channels, out_channels, kernel_size, n_groups=n_groups),
        ])

        # FiLM modulation https://arxiv.org/abs/1709.07871
        # predicts per-channel scale and bias
        cond_channels = out_channels * 2
        self.out_channels = out_channels
        self.cond_encoder = nn.Sequential(
            nn.Mish(),
            nn.Linear(cond_dim, cond_channels),
            nn.Unflatten(-1, (-1, 1))
        )

        # make sure dimensions compatible
        self.residual_conv = nn.Conv1d(in_channels, out_channels, 1) \
            if in_channels != out_channels else nn.Identity()

    def forward(self, x, cond):
        '''
            x : [ batch_size x in_channels x horizon ]
            cond : [ batch_size x cond_dim]

            returns:
            out : [ batch_size x out_channels x horizon ]
        '''
        out = self.blocks[0](x)
        embed = self.cond_encoder(cond)

        embed = embed.reshape(
            embed.shape[0], 2, self.out_channels, 1)
        scale = embed[:,0,...]
        bias = embed[:,1,...]
        out = scale * out + bias

        out = self.blocks[1](out)
        out = out + self.residual_conv(x)
        return out


class ConditionalUnet1D(nn.Module):
    def __init__(self,
        input_dim,
        global_cond_dim,
        diffusion_step_embed_dim=256,
        down_dims=[256,512,1024],
        kernel_size=5,
        n_groups=8
        ):
        """
        input_dim: Dim of actions.
        global_cond_dim: Dim of global conditioning applied with FiLM
          in addition to diffusion step embedding. This is usually obs_horizon * obs_dim
        diffusion_step_embed_dim: Size of positional encoding for diffusion iteration k
        down_dims: Channel size for each UNet level.
          The length of this array determines numebr of levels.
        kernel_size: Conv kernel size
        n_groups: Number of groups for GroupNorm
        """

        super().__init__()
        all_dims = [input_dim] + list(down_dims)
        start_dim = down_dims[0]

        dsed = diffusion_step_embed_dim
        diffusion_step_encoder = nn.Sequential(
            SinusoidalPosEmb(dsed),
            nn.Linear(dsed, dsed * 4),
            nn.Mish(),
            nn.Linear(dsed * 4, dsed),
        )
        cond_dim = dsed + global_cond_dim

        in_out = list(zip(all_dims[:-1], all_dims[1:]))
        mid_dim = all_dims[-1]
        self.mid_modules = nn.ModuleList([
            ConditionalResidualBlock1D(
                mid_dim, mid_dim, cond_dim=cond_dim,
                kernel_size=kernel_size, n_groups=n_groups
            ),
            ConditionalResidualBlock1D(
                mid_dim, mid_dim, cond_dim=cond_dim,
                kernel_size=kernel_size, n_groups=n_groups
            ),
        ])

        down_modules = nn.ModuleList([])
        for ind, (dim_in, dim_out) in enumerate(in_out):
            is_last = ind >= (len(in_out) - 1)
            down_modules.append(nn.ModuleList([
                ConditionalResidualBlock1D(
                    dim_in, dim_out, cond_dim=cond_dim,
                    kernel_size=kernel_size, n_groups=n_groups),
                ConditionalResidualBlock1D(
                    dim_out, dim_out, cond_dim=cond_dim,
                    kernel_size=kernel_size, n_groups=n_groups),
                Downsample1d(dim_out) if not is_last else nn.Identity()
            ]))

        up_modules = nn.ModuleList([])
        for ind, (dim_in, dim_out) in enumerate(reversed(in_out[1:])):
            is_last = ind >= (len(in_out) - 1)
            up_modules.append(nn.ModuleList([
                ConditionalResidualBlock1D(
                    dim_out*2, dim_in, cond_dim=cond_dim,
                    kernel_size=kernel_size, n_groups=n_groups),
                ConditionalResidualBlock1D(
                    dim_in, dim_in, cond_dim=cond_dim,
                    kernel_size=kernel_size, n_groups=n_groups),
                Upsample1d(dim_in) if not is_last else nn.Identity()
            ]))

        final_conv = nn.Sequential(
            Conv1dBlock(start_dim, start_dim, kernel_size=kernel_size),
            nn.Conv1d(start_dim, input_dim, 1),
        )

        self.diffusion_step_encoder = diffusion_step_encoder
        self.up_modules = up_modules
        self.down_modules = down_modules
        self.final_conv = final_conv

        print("number of parameters: {:e}".format(
            sum(p.numel() for p in self.parameters()))
        )

    def forward(self,
            sample: torch.Tensor,
            timestep: Union[torch.Tensor, float, int],
            global_cond=None):
        """
        x: (B,T,input_dim)
        timestep: (B,) or int, diffusion step
        global_cond: (B,global_cond_dim)
        output: (B,T,input_dim)
        """
        # (B,T,C)
        sample = sample.moveaxis(-1,-2)
        # (B,C,T)

        # 1. time
        timesteps = timestep
        if not torch.is_tensor(timesteps):
            # TODO: this requires sync between CPU and GPU. So try to pass timesteps as tensors if you can
            timesteps = torch.tensor([timesteps], dtype=torch.long, device=sample.device)
        elif torch.is_tensor(timesteps) and len(timesteps.shape) == 0:
            timesteps = timesteps[None].to(sample.device)
        # broadcast to batch dimension in a way that's compatible with ONNX/Core ML
        timesteps = timesteps.expand(sample.shape[0])

        global_feature = self.diffusion_step_encoder(timesteps)

        if global_cond is not None:
            global_feature = torch.cat([
                global_feature, global_cond
            ], axis=-1)

        x = sample
        h = []
        for idx, (resnet, resnet2, downsample) in enumerate(self.down_modules):
            x = resnet(x, global_feature)
            x = resnet2(x, global_feature)
            h.append(x)
            x = downsample(x)

        for mid_module in self.mid_modules:
            x = mid_module(x, global_feature)

        for idx, (resnet, resnet2, upsample) in enumerate(self.up_modules):
            x = torch.cat((x, h.pop()), dim=1)
            x = resnet(x, global_feature)
            x = resnet2(x, global_feature)
            x = upsample(x)

        x = self.final_conv(x)

        # (B,C,T)
        x = x.moveaxis(-1,-2)
        # (B,T,C)
        return x



# download demonstration data from Google Drive
# dataset_path = "pusht_cchi_v7_replay.zarr.zip"
# if not os.path.isfile(dataset_path):
#     id = "1KY1InLurpMvJDRb14L9NlXT_fEsCvVUq&confirm=t"
#     gdown.download(id=id, output=dataset_path, quiet=False)

dataset_path = "/home/jeon/jeon_ws/diffusion_policy/src/diffusion_cam/clock1_98_delta.zarr.zip"

import timm
#@markdown ### **Network Demo**
class DiffusionPolicy_Real:     
    def __init__(self,
                train=True, 
                encoder = "resnet", 
                action_def = "delta", 
                force_mod:bool = False, 
                single_view:bool = False, 
                force_encode = True,
                cross_attn: bool = False):
        # action dimension should also correspond with the state dimension (x,y,z, x, y, z, w)
        action_dim = 9
        # parameters
        pred_horizon = 16
        obs_horizon = 2
        action_horizon = 8
        #|o|o|                             observations: 2
        #| |a|a|a|a|a|a|a|a|               actions executed: 8
        #|p|p|p|p|p|p|p|p|p|p|p|p|p|p|p|p| actions predicted: 16
        batch_size = 32
        Transformer_bool = None
        modality = "without_force"
        view = "dual_view"
        if force_mod:
            modality = "with_force"
        if single_view:
            view = "single_view"
        # construct ResNet18 encoder
        # if you have multiple camera views, use seperate encoder weights for each view.
        # Resnet18 and resnet34 both have same dimension for the output
        # Define Second vision encoder
        if encoder == "resnet":
           print("")
           vision_encoder2 = train_utils().get_resnet('resnet18')
        elif encoder == "Transformer":
            Transformer_bool = True
            print("Imported Transformer clip model")
            vision_encoder2 = timm.create_model('vit_base_patch16_clip_224.openai', pretrained=True)
        if not single_view:
            vision_encoder = train_utils().get_resnet('resnet18')
            vision_encoder = train_utils().replace_bn_with_gn(vision_encoder)
        if force_encode:
            force_encoder = ForceEncoder(4, 512, batch_size = batch_size, obs_horizon = obs_horizon, cross_attn=cross_attn)
        # IMPORTANT!
        # replace all BatchNorm with GroupNorm to work with EMA
        # performance will tank if you forget to do this!
        vision_encoder2 = train_utils().replace_bn_with_gn(vision_encoder2)
        # ResNet18 has output dim of 512 X 2 because two views
        if single_view:
            vision_feature_dim = 512
        else:
            vision_feature_dim = 512 + 512
        if force_encode:
            force_feature_dim = 512
        else:
            force_feature_dim = 4
        # agent_pos is seven (x,y,z, w, y, z, w ) dimensional
        lowdim_obs_dim = 9
        # observation feature has 514 dims in total per step
        if force_mod:
            obs_dim = vision_feature_dim + force_feature_dim  + lowdim_obs_dim
        else:            
            obs_dim = vision_feature_dim + lowdim_obs_dim

        if train:
            # create dataset from file
            dataset = RealRobotDataSet(
                dataset_path=dataset_path,
                pred_horizon=pred_horizon,
                obs_horizon=obs_horizon,
                action_horizon=action_horizon,
                Transformer= Transformer_bool,
                force_mod = force_mod,
                single_view=single_view
            )
            # save training data statistics (min, max) for each dim
            stats = dataset.stats

           # Save the stats to a file
            with open(f'stats_clock_clean_{encoder}_{action_def}_{modality}.json', 'w') as f:
                json.dump(stats, f, cls=NumpyEncoder)
                print("stats saved")
            # create dataloader
            dataloader = torch.utils.data.DataLoader(
                dataset,
                batch_size=batch_size,
                num_workers=4,
                shuffle=True,
                # accelerate cpu-gpu transfer
                pin_memory=True,
                # don't kill worker process afte each epoch
                persistent_workers=True
            )

            self.dataloader = dataloader
            self.stats = stats

        #### For debugging purposes uncomment
        # import matplotlib.pyplot as plt
        # imdata = dataset[100]['image']
        # if imdata.dtype == np.float32 or imdata.dtype == np.float64:
        #     imdata = imdata / 255.0
        # img1 = imdata[0]
        # img2 = imdata[1]
        # # Loop through the two different "channels"
        # fig, axes = plt.subplots(1, 2, figsize=(10, 5))
        # for i in range(2):
        #     # Convert the 3x96x96 tensor to a 96x96x3 image (for display purposes)
        #     img = np.transpose(imdata[i], (1, 2, 0))
            
        #     # Display the image in the i-th subplot
        #     axes[i].imshow(img)
        #     axes[i].set_title(f'Channel {i + 1}')
        #     axes[i].axis('off')

        # # Show the plot
        # plt.show()  

        # # Check if both images are exactly the same
        # are_equal = np.array_equal(img1, img2)

        # if are_equal:
        #     print("The images are the same.")
        # else:
        #     print("The images are different.")
        ######### End ########
            # visualize data in batch
            batch = next(iter(dataloader))
            print("batch['image'].shape:", batch['image'].shape)
            if not single_view:
                print("batch[image2].shape", batch["image2"].shape)

            print("batch['agent_pos'].shape:", batch['agent_pos'].shape)
            
            if force_mod:
                print("batch['force'].shape:", batch['force'].shape)

            print("batch['action'].shape", batch['action'].shape)
            self.batch = batch

        # create network object
        noise_pred_net = ConditionalUnet1D(
            input_dim=action_dim,
            global_cond_dim=obs_dim*obs_horizon
        )
        if single_view:
            # the final arch has 2 parts
            nets = nn.ModuleDict({
                'vision_encoder': vision_encoder2,
                'noise_pred_net': noise_pred_net
            })
        elif single_view and force_encode:
            # the final arch has 2 parts
            nets = nn.ModuleDict({
                'vision_encoder': vision_encoder2,
                'force_encoder': force_encoder,
                'noise_pred_net': noise_pred_net
            })   
        elif not single_view and force_encode:
            nets = nn.ModuleDict({
                'vision_encoder': vision_encoder,
                'vision_encoder2': vision_encoder2,
                'force_encoder': force_encoder,
                'noise_pred_net': noise_pred_net
            })
        elif not single_view and not force_encode:
            nets = nn.ModuleDict({
                'vision_encoder': vision_encoder,
                'vision_encoder2': vision_encoder2,
                'noise_pred_net': noise_pred_net
            })

        # diffusion iteration
        num_diffusion_iters = 100

        noise_scheduler = DDPMScheduler(
            num_train_timesteps=num_diffusion_iters,
            # the choise of beta schedule has big impact on performance
            # we found squared cosine works the best
            beta_schedule='squaredcos_cap_v2',
            # clip output to [-1,1] to improve stability
            clip_sample=True,
            # our network predicts noise (instead of denoised action)
            prediction_type='epsilon'
        )

        
        self.nets = nets
        self.noise_scheduler = noise_scheduler
        self.num_diffusion_iters = num_diffusion_iters
        self.obs_horizon = obs_horizon
        self.obs_dim = obs_dim
        if not single_view:
            self.vision_encoder = vision_encoder

        self.vision_encoder2 = vision_encoder2
        if force_encode:
            self.force_encoder = force_encoder
        self.noise_pred_net = noise_pred_net
        self.action_horizon = action_horizon
        self.pred_horizon = pred_horizon
        self.lowdim_obs_dim = lowdim_obs_dim
        self.action_dim = action_dim






class DiffusionPolicy_Real_SingleView:     
    def __init__(self, train=True):

        # construct ResNet18 encoder
        # if you have multiple camera views, use seperate encoder weights for each view.
        # Resnet18 and resnet34 both have same dimension for the output
        vision_encoder = train_utils().get_resnet('resnet18')
        # Define Second vision encoder
        # IMPORTANT!
        # replace all BatchNorm with GroupNorm to work with EMA
        # performance will tank if you forget to do this!
        vision_encoder = train_utils().replace_bn_with_gn(vision_encoder)
        # ResNet18 has output dim of 512 X 2 because two views
        vision_feature_dim = 512
        # agent_pos is seven (x,y,z, w, y, z, w ) dimensional
        lowdim_obs_dim = 7
        # observation feature has 514 dims in total per step
        obs_dim = vision_feature_dim + lowdim_obs_dim
        # action dimension should also correspond with the state dimension (x,y,z, x, y, z, w)
        action_dim = 7
        # parameters
        pred_horizon = 16
        obs_horizon = 2
        action_horizon = 8
        #|o|o|                             observations: 2
        #| |a|a|a|a|a|a|a|a|               actions executed: 8
        #|p|p|p|p|p|p|p|p|p|p|p|p|p|p|p|p| actions predicted: 16
        if train:
            # create dataset from file
            dataset = RealRobotDataSet_SingleView(
                dataset_path=dataset_path,
                pred_horizon=pred_horizon,
                obs_horizon=obs_horizon,
                action_horizon=action_horizon
            )
            # save training data statistics (min, max) for each dim
            stats = dataset.stats
           # Save the stats to a file
            with open('stats_clock_clean_resnet.json', 'w') as f:
                json.dump(stats, f, cls=NumpyEncoder)
                print("stats saved")
            # create dataloader
            dataloader = torch.utils.data.DataLoader(
                dataset,
                batch_size=64,
                num_workers=4,
                shuffle=True,
                # accelerate cpu-gpu transfer
                pin_memory=True,
                # don't kill worker process afte each epoch
                persistent_workers=True
            )

            self.dataloader = dataloader
            self.stats = stats

        #### For debugging purposes uncomment
        # import matplotlib.pyplot as plt
        # imdata = dataset[100]['image']
        # if imdata.dtype == np.float32 or imdata.dtype == np.float64:
        #     imdata = imdata / 255.0
        # img1 = imdata[0]
        # img2 = imdata[1]
        # # Loop through the two different "channels"
        # fig, axes = plt.subplots(1, 2, figsize=(10, 5))
        # for i in range(2):
        #     # Convert the 3x96x96 tensor to a 96x96x3 image (for display purposes)
        #     img = np.transpose(imdata[i], (1, 2, 0))
            
        #     # Display the image in the i-th subplot
        #     axes[i].imshow(img)
        #     axes[i].set_title(f'Channel {i + 1}')
        #     axes[i].axis('off')

        # # Show the plot
        # plt.show()  

        # # Check if both images are exactly the same
        # are_equal = np.array_equal(img1, img2)

        # if are_equal:
        #     print("The images are the same.")
        # else:
        #     print("The images are different.")
        ######### End ########

        
            # visualize data in batch
            batch = next(iter(dataloader))
            print("batch['image'].shape:", batch['image'].shape)
            print("batch['agent_pos'].shape:", batch['agent_pos'].shape)
            print("batch['action'].shape", batch['action'].shape)
            self.batch = batch

        # create network object
        noise_pred_net = ConditionalUnet1D(
            input_dim=action_dim,
            global_cond_dim=obs_dim*obs_horizon
        )

        # the final arch has 2 parts
        nets = nn.ModuleDict({
            'vision_encoder': vision_encoder,
            'noise_pred_net': noise_pred_net
        })
        # diffusion iteration
        num_diffusion_iters = 100

        noise_scheduler = DDPMScheduler(
            num_train_timesteps=num_diffusion_iters,
            # the choise of beta schedule has big impact on performance
            # we found squared cosine works the best
            beta_schedule='squaredcos_cap_v2',
            # clip output to [-1,1] to improve stability
            clip_sample=True,
            # our network predicts noise (instead of denoised action)
            prediction_type='epsilon'
        )

        
        self.nets = nets
        self.noise_scheduler = noise_scheduler
        self.num_diffusion_iters = num_diffusion_iters
        self.obs_horizon = obs_horizon
        self.obs_dim = obs_dim
        self.vision_encoder = vision_encoder
        self.noise_pred_net = noise_pred_net
        self.action_horizon = action_horizon
        self.pred_horizon = pred_horizon
        self.lowdim_obs_dim = lowdim_obs_dim
        self.action_dim = action_dim
# # demo
# with torch.no_grad():
#     # example inputs
#     image = torch.zeros((1, obs_horizon,3,96,96))
#     agent_pos = torch.zeros((1, obs_horizon, 2))
#     # vision encoder
#     image_features = nets['vision_encoder'](
#         image.flatten(end_dim=1))
#     # (2,512)
#     image_features = image_features.reshape(*image.shape[:2],-1)
#     # (1,2,512)
#     obs = torch.cat([image_features, agent_pos],dim=-1)
#     # (1,2,514)

#     noised_action = torch.randn((1, pred_horizon, action_dim))
#     diffusion_iter = torch.zeros((1,))

#     # the noise prediction network
#     # takes noisy action, diffusion iteration and observation as input
#     # predicts the noise added to action
#     noise = nets['noise_pred_net'](
#         sample=noised_action,
#         timestep=diffusion_iter,
#         global_cond=obs.flatten(start_dim=1))

#     # illustration of removing noise
#     # the actual noise removal is performed by NoiseScheduler
#     # and is dependent on the diffusion noise schedule
#     denoised_action = noised_action - noise


def test():
    # create dataset from file
    obs_horizon = 2 
    dataset = RealRobotDataSet(
        dataset_path=dataset_path,
        pred_horizon=16,
        obs_horizon=obs_horizon,
        action_horizon=8,
        Transformer= False,
        force_mod = True,
        single_view= True
    )
    # save training data statistics (min, max) for each dim
    stats = dataset.stats

    batch_size = 10
    # create dataloader
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        num_workers=4,
        shuffle=True,
        # accelerate cpu-gpu transfer
        pin_memory=True,
        # don't kill worker process afte each epoch
        persistent_workers=True
    )
    
    batch = next(iter(dataloader))
    print("batch['image'].shape:", batch['image'].shape)

    # ### For debugging purposes uncomment
    # import matplotlib.pyplot as plt
    # imdata = dataset[100]['image']
    # if imdata.dtype == np.float32 or imdata.dtype == np.float64:
    #     imdata = imdata
    # img1 = imdata[0]
    # img2 = imdata[1]
    # # Loop through the two different "channels"
    # fig, axes = plt.subplots(1, 2, figsize=(10, 5))
    # for i in range(2):
    #     # Convert the 3x96x96 tensor to a 96x96x3 image (for display purposes)
    #     img = np.transpose(imdata[i], (1, 2, 0))
        
    #     # Display the image in the i-th subplot
    #     axes[i].imshow(img)
    #     axes[i].set_title(f'Channel {i + 1}')
    #     axes[i].axis('off')

    # # Show the plot
    # plt.show()  
    
    print("batch['agent_pos'].shape:", batch['agent_pos'].shape)    
    print("batch['force'].shape:", batch['force'].shape)
    print("batch['action'].shape", batch['action'].shape)
    image_input_shape  = (3, 320, 240)
    force_dim = 4
    hidden_dim = 512

    import torch.optim as optim
    device = torch.device('cuda')
    # Standard ADAM optimizer
    # Note that EMA parametesr are not optimized
    model = CrossAttentionFusion(image_input_shape, force_dim, hidden_dim, batch_size = batch_size, obs_horizon=obs_horizon, resnet= True)
    model = model.to(device)
    num_epochs = 10  # Set the number of epochs
    nimage = batch['image'][:,:2].to(device)
    nforce = batch['force'][:,:2].to(device)
    for epoch in range(num_epochs):
        # Example random input data for demonstration
        image_input = nimage.flatten(end_dim=1).to(device)  # Batch of 8 images
        force_input = nforce.flatten(end_dim=1).to(device)
        # Batch of 8 force vectors

        # Forward pass
        latent_embedding = model(image_input, force_input, batch_size, obs_horizon)


        print(f'Epoch [{epoch+1}/{num_epochs}. {latent_embedding}')
##TODO: Make sure that new CNN can work with the new architecture for CrossAttention
test()

