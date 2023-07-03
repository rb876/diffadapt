import random
import json
import argparse
import yaml 
import torch 
import numpy as np 
import matplotlib.pyplot as plt
from itertools import islice
from PIL import Image
from omegaconf import OmegaConf
import numpy as np 
import h5py 
import os 
from torchvision.transforms import Resize
from pathlib import Path


from src import (get_standard_sde, PSNR, SSIM, get_standard_dataset, get_data_from_ground_truth, get_standard_ray_trafo,  
	get_standard_score, get_standard_sampler, get_standard_configs, get_standard_path, MulticoilMRI) 

parser = argparse.ArgumentParser(description='conditional sampling')
parser.add_argument('--model', default='openai_unet', help='select unet arch.', choices=['dds_unet', 'openai_unet'])
parser.add_argument('--method',  default='dds', choices=['naive', 'dps', 'dds'])
parser.add_argument('--add_corrector_step', action='store_true')
parser.add_argument('--num_steps', default=100)
parser.add_argument('--penalty', default=1, help='reg. penalty used for ``naive'' and ``dps'' only.')
parser.add_argument('--gamma', default=0.01, help='reg. used for ``dds''.')
parser.add_argument('--eta', default=0.15, help='reg. used for ``dds'' weighting stochastic and deterministic noise.')
parser.add_argument('--pct_chain_elapsed', default=0,  help='``pct_chain_elapsed'' actives init of chain')
parser.add_argument('--sde', default='ddpm', choices=['vpsde', 'vesde', 'ddpm'])
parser.add_argument('--cg_iter', default=5)
parser.add_argument('--load_path', help='path to ddpm model.')
parser.add_argument('--base_path', help='path to ddpm model configs.')
parser.add_argument('--anatomy', default="knee",
					choices=["knee", "brain"])
parser.add_argument('--mask_type', default="uniform1d",
					choices=["uniform1d", "gaussian1d", "poisson"])
parser.add_argument('--acc_factor', type=int, default=4)


def get_mask(img, size, batch_size, type='gaussian2d', acc_factor=8, center_fraction=0.04, fix=False):
	mux_in = size ** 2
	if type.endswith('2d'):
		Nsamp = mux_in // acc_factor
	elif type.endswith('1d'):
		Nsamp = size // acc_factor
	if type == 'gaussian2d':
		mask = torch.zeros_like(img)
		cov_factor = size * (1.5 / 128)
		mean = [size // 2, size // 2]
		cov = [[size * cov_factor, 0], [0, size * cov_factor]]
		if fix:
			samples = np.random.multivariate_normal(mean, cov, int(Nsamp))
			int_samples = samples.astype(int)
			int_samples = np.clip(int_samples, 0, size - 1)
			mask[..., int_samples[:, 0], int_samples[:, 1]] = 1
		else:
			for i in range(batch_size):
				# sample different masks for batch
				samples = np.random.multivariate_normal(mean, cov, int(Nsamp))
				int_samples = samples.astype(int)
				int_samples = np.clip(int_samples, 0, size - 1)
				mask[i, :, int_samples[:, 0], int_samples[:, 1]] = 1
	elif type == 'uniformrandom2d':
		mask = torch.zeros_like(img)
		if fix:
			mask_vec = torch.zeros([1, size * size])
			samples = np.random.choice(size * size, int(Nsamp))
			mask_vec[:, samples] = 1
			mask_b = mask_vec.view(size, size)
			mask[:, ...] = mask_b
		else:
			for i in range(batch_size):
				# sample different masks for batch
				mask_vec = torch.zeros([1, size * size])
				samples = np.random.choice(size * size, int(Nsamp))
				mask_vec[:, samples] = 1
				mask_b = mask_vec.view(size, size)
				mask[i, ...] = mask_b
	elif type == 'gaussian1d':
		mask = torch.zeros_like(img)
		mean = size // 2
		std = size * (15.0 / 128)
		Nsamp_center = int(size * center_fraction)
		if fix:
			samples = np.random.normal(
				loc=mean, scale=std, size=int(Nsamp * 1.2))
			int_samples = samples.astype(int)
			int_samples = np.clip(int_samples, 0, size - 1)
			mask[..., int_samples] = 1
			c_from = size // 2 - Nsamp_center // 2
			mask[..., c_from:c_from + Nsamp_center] = 1
		else:
			for i in range(batch_size):
				samples = np.random.normal(
					loc=mean, scale=std, size=int(Nsamp*1.2))
				int_samples = samples.astype(int)
				int_samples = np.clip(int_samples, 0, size - 1)
				mask[i, :, :, int_samples] = 1
				c_from = size // 2 - Nsamp_center // 2
				mask[i, :, :, c_from:c_from + Nsamp_center] = 1
	elif type == 'uniform1d':
		mask = torch.zeros_like(img)
		if fix:
			Nsamp_center = int(size * center_fraction)
			samples = np.random.choice(size, int(Nsamp - Nsamp_center))
			mask[..., samples] = 1
			# ACS region
			c_from = size // 2 - Nsamp_center // 2
			mask[..., c_from:c_from + Nsamp_center] = 1
		else:
			for i in range(batch_size):
				Nsamp_center = int(size * center_fraction)
				samples = np.random.choice(size, int(Nsamp - Nsamp_center))
				mask[i, :, :, samples] = 1
				# ACS region
				c_from = size // 2 - Nsamp_center // 2
				mask[i, :, :, c_from:c_from+Nsamp_center] = 1
	else:
		NotImplementedError(f'Mask type {type} is currently not supported.')

	return mask


def real_to_nchw_comp(x):
	"""
	[1, 2, 320, 320] real --> [1, 1, 320, 320] comp
	"""
	if len(x.shape) == 4:
		x = x[:, 0:1, :, :] + x[:, 1:2, :, :] * 1j
	elif len(x.shape) == 3:
		x = x[0:1, :, :] + x[1:2, :, :] * 1j
	return x


def seed_everything(seed: int = 42):
	random.seed(seed)
	np.random.seed(seed)
	os.environ["PYTHONHASHSEED"] = str(seed)
	torch.manual_seed(seed)
	torch.cuda.manual_seed(seed)  # type: ignore
	torch.backends.cudnn.deterministic = True  # type: ignore
	torch.backends.cudnn.benchmark = True  # type: ignore

def coordinator(args):
	
	seed_everything()
	
	with open(args.base_path, 'r') as stream:
		config = yaml.load(stream, Loader=yaml.UnsafeLoader)
		config = OmegaConf.create(config)

	model_type = "dds_unet" 
	sde = get_standard_sde(config=config)
	score = get_standard_score(config=config, sde=sde, 
			use_ema=False, model_type="dds_unet", load_model=False)
	score.load_state_dict(torch.load(args.load_path))
	print(f'Model ckpt loaded from {args.load_path}')
	score.convert_to_fp32()
	score.dtype = torch.float32

	score = score.to(config.device)
	score.to("cuda")
	score.eval()
 
	size = 256

	if args.anatomy == "knee":
		# vol_list = ["file1000007", "file1000017", "file1000026", "file1000033", "file1000041", \
        #       		"file1000052", "file1000071", "file1000073", "file1000107", "file1000108"]
		vol_list = ["file1000033"]
		root = Path(f"/media/harry/tomo/fastmri/knee_mvue_{size}_val")
		# data_path = f"/media/harry/tomo/fastmri/knee_mvue_{size}_val/file1000033/slice"
		# data_path_mps = f"/media/harry/tomo/fastmri/knee_mvue_{size}_val/file1000033/mps"
		# x = np.load(os.path.join(data_path, "015.npy"))
		# mps = np.load(os.path.join(data_path_mps, "015.npy"))
	elif args.anatomy == "brain":
		data_path = f"/media/harry/tomo/fastmri/brain_mvue_{size}_val/file_brain_AXT2_200_2000019/slice"
		data_path_mps = f"/media/harry/tomo/fastmri/brain_mvue_{size}_val/file_brain_AXT2_200_2000019/mps"
		# x = np.load(os.path.join(data_path, "005.npy"))
		# mps = np.load(os.path.join(data_path_mps, "005.npy"))

	"""
	Iterate over dataset
	"""
	save_root = Path(f"./results_noadapt/{args.anatomy}/{args.mask_type}_acc{args.acc_factor}")
	save_root.mkdir(exist_ok=True, parents=True)
 
 
	cnt = 0
	psnr_avg = 0
	ssim_avg = 0
	for vol in vol_list:
     
		for t in ["input", "recon", "label", "mask"]:
			(save_root / t / f"{vol}").mkdir(exist_ok=True, parents=True)
     
		print(vol)
		data_path = root / f"{vol}" / "slice"
		data_path_mps = root / f"{vol}" / "mps"
  
		list_fname = sorted(list(data_path.glob("*.npy")))
		for f in list_fname:
			fname = str(f).split('/')[-1][:-4]
			x = np.load(os.path.join(data_path, f"{fname}.npy"))
			mps = np.load(os.path.join(data_path_mps, f"{fname}.npy"))

			mask = get_mask(torch.zeros([1, 1, size, size]), size, 
										1, type=args.mask_type,
										acc_factor=args.acc_factor, center_fraction=0.08)
			mask = mask.to(config.device)
			Ncoil, _, _ = mps.shape
		
			mps = torch.from_numpy(mps)
			mps = mps.view(1, Ncoil, size, size).to(config.device)

			ray_trafo = MulticoilMRI(mask=mask, sens=mps)
			x_torch = torch.from_numpy(x).unsqueeze(0).unsqueeze(0)
		
			ground_truth = x_torch
			ground_truth = ground_truth.to(config.device)
			print("RANGE OF GT: ", torch.abs(ground_truth).min(), torch.abs(ground_truth).max())

			observation = ray_trafo.trafo(ground_truth)

			filtbackproj = ray_trafo.fbp(observation)

			print(filtbackproj.shape, ground_truth.shape, observation.shape)


			logg_kwargs = {'log_dir': ".", 'num_img_in_log': 5,
				'sample_num':0, 'ground_truth': ground_truth, 'filtbackproj': filtbackproj}
			sampler = get_standard_sampler(
				args=args,
				config=config,
				score=score,
				sde=sde,
				ray_trafo=ray_trafo,
				filtbackproj=filtbackproj,
				observation=observation,
				device=config.device
				)
			
			recon = sampler.sample(logg_kwargs=logg_kwargs, logging=False)
			print("FINISHED SAMPLING")
			print(recon.shape)

			recon = real_to_nchw_comp(recon)
			recon = np.abs(recon.detach().cpu().numpy())
		
			meas_img = np.abs(filtbackproj.detach().cpu().numpy())

			psnr = PSNR(recon[0, 0], np.abs(ground_truth[0, 0].cpu().numpy()))
			ssim = SSIM(recon[0, 0], np.abs(ground_truth[0, 0].cpu().numpy()), data_range=np.abs(ground_truth[0, 0].cpu().numpy()).max())
			
			psnr_avg += psnr
			ssim_avg += ssim
			
			import matplotlib.pyplot as plt
			plt.imsave(str(save_root / "input" / f"{vol}" / f"{fname}.png"), meas_img[0, 0], cmap="gray")
			plt.imsave(str(save_root / "mask" / f"{vol}" / f'{fname}.png'), mask[0,0,:,:].detach().cpu(), cmap='gray')
			plt.imsave(str(save_root / "recon" / f"{vol}" / f"{fname}.png"), recon[0, 0], cmap="gray")
			plt.imsave(str(save_root / "label" / f"{vol}" / f"{fname}.png"), np.abs(ground_truth[0, 0].cpu().numpy()), cmap="gray")
			cnt += 1
		
	summary = {}
	psnr_avg /= cnt
	ssim_avg /= cnt
	summary["results"] = {"PSNR": psnr_avg, "SSIM": ssim_avg}
	with open(str(save_root / f"summary.json"), 'w') as f:
		json.dump(summary, f)


if __name__ == '__main__':
	args = parser.parse_args()
	coordinator(args)