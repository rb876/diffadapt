import argparse
import yaml 
import torch 
import numpy as np 
import matplotlib.pyplot as plt
from itertools import islice
from PIL import Image

from src import (get_standard_sde, PSNR, SSIM, get_standard_dataset, get_data_from_ground_truth, get_standard_ray_trafo,  
	get_standard_score, get_standard_sampler, get_standard_configs, get_standard_path) 

parser = argparse.ArgumentParser(description='conditional sampling')
parser.add_argument('--dataset', default='walnut', help='test-dataset', choices=['walnut', 'lodopab', 'ellipses', 'mayo', 'aapm'])
parser.add_argument('--model', default='openai_unet', help='select unet arch.', choices=['dds_unet', 'openai_unet'])
parser.add_argument('--base_path', default='/localdata/AlexanderDenker/score_based_baseline', help='path to model configs')
parser.add_argument('--model_learned_on', default='lodopab', help='model-checkpoint to load', choices=['lodopab', 'ellipses', 'aapm', "knee"])
parser.add_argument('--version', default=1, help="version of the model")
parser.add_argument('--method',  default='naive', choices=['naive', 'dps', 'dds'])
parser.add_argument('--add_corrector_step', action='store_true')
parser.add_argument('--ema', action='store_true')
parser.add_argument('--num_steps', default=1000)
parser.add_argument('--penalty', default=1, help='reg. penalty used for ``naive'' and ``dps'' only.')
parser.add_argument('--gamma', default=0.01, help='reg. used for ``dds''.')
parser.add_argument('--eta', default=0.15, help='reg. used for ``dds'' weighting stochastic and deterministic noise.')
parser.add_argument('--pct_chain_elapsed', default=0,  help='``pct_chain_elapsed'' actives init of chain')
parser.add_argument('--sde', default='vesde', choices=['vpsde', 'vesde', 'ddpm'])
parser.add_argument('--cg_iter', default=5)
parser.add_argument('--load_path', help='path to ddpm model.')
parser.add_argument('--stddev', default=None, help="noise_level")
parser.add_argument('--early_stopping_pct', default=1.0, help="early stop sampling. Only used for DDPM and DPS.")

def coordinator(args):
	config, dataconfig = get_standard_configs(args, base_path=args.base_path)
	try:
		save_root = get_standard_path(args, run_type=args.method, data_part=dataconfig.data.part)
	except AttributeError:
		save_root = get_standard_path(args, run_type=args.method)
	print("save to: ", save_root)
	save_root.mkdir(parents=True, exist_ok=True)

	if config.seed is not None:
		torch.manual_seed(config.seed) # for reproducible noise in simulate

	dataconfig.data.stddev = dataconfig.data.stddev if args.stddev == None else float(args.stddev)

	sde = get_standard_sde(config=config)
	score = get_standard_score(config=config, sde=sde, use_ema=args.ema, model_type=args.model)
	score = score.to(config.device).eval()
	ray_trafo = get_standard_ray_trafo(config=dataconfig)
	ray_trafo = ray_trafo.to(device=config.device)
	dataset = get_standard_dataset(config=dataconfig, ray_trafo=ray_trafo)
	print("Number of parameters: ", sum([p.numel() for p in score.parameters()]))
	_psnr, _ssim = [], []
	for i, data_sample in enumerate(islice(dataset, dataconfig.data.validation.num_images)):
		if config.seed is not None:
			torch.manual_seed(config.seed + i)  # for reproducible noise in simulate
		if len(data_sample) == 3:
			observation, ground_truth, filtbackproj = data_sample
			ground_truth = ground_truth.to(device=config.device)
			observation = observation.to(device=config.device)
			filtbackproj = filtbackproj.to(device=config.device)
		else:
			if len(data_sample) == 1 and args.dataset == "ellipses" and dataconfig.data.part == "test":
				data_sample = data_sample[0]
			ground_truth, observation, filtbackproj = get_data_from_ground_truth(
				ground_truth=data_sample.to(device=config.device),
				ray_trafo=ray_trafo,
				white_noise_rel_stddev=dataconfig.data.stddev
				)

		logg_kwargs = {'log_dir': save_root, 'num_img_in_log': 1,
			'sample_num':i, 'ground_truth': ground_truth, 'filtbackproj': filtbackproj}
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
		
		recon = sampler.sample(logg_kwargs=logg_kwargs)
		recon = torch.clamp(recon, 0)
		torch.save(		{'recon': recon.cpu().squeeze(), 'ground_truth': ground_truth.cpu().squeeze()}, 
			str(save_root / f'recon_{i}_info.pt')	)
		im = Image.fromarray(recon.cpu().squeeze().numpy()*255.).convert("L")
		im.save(str(save_root / f'recon_{i}.png'))

		print(f'reconstruction of sample {i}'	)
		psnr = PSNR(recon[0, 0].cpu().numpy(), ground_truth[0, 0].cpu().numpy())
		ssim = SSIM(recon[0, 0].cpu().numpy(), ground_truth[0, 0].cpu().numpy())	
		print('PSNR:', psnr)
		print('SSIM:', ssim)
		_psnr.append(psnr)
		_ssim.append(ssim)
		
		#fig, (ax1, ax2, ax3) = plt.subplots(1,3)
		#im = ax1.imshow(ground_truth[0,0,:,:].detach().cpu(), cmap='gray')
		#fig.colorbar(im, ax=ax1)
		#ax1.axis('off')
		#ax1.set_title('Ground truth')
		#im = ax2.imshow(torch.clamp(recon[0,0,:,:], 0, 1).detach().cpu(), cmap='gray')
		#fig.colorbar(im, ax=ax2)
		#ax2.axis('off')
		#ax2.set_title(args.method)
		#ax3.imshow(filtbackproj[0,0,:,:].detach().cpu(), cmap='gray')
		#ax3.axis('off')
		#ax3.set_title('FBP')
		#plt.savefig(str(save_root/f'info_{i}.png')) 
		#plt.show() 
		
		#plt.show()
	report = {}
	report.update(dict(dataconfig.items()))
	report.update(vars(args))
	report['PSNR'] = float(np.mean(_psnr))
	report['SSIM'] = float(np.mean(_ssim))

	with open(save_root / 'report.yaml', 'w') as file:
		yaml.dump(report, file)

if __name__ == '__main__':
	args = parser.parse_args()
	coordinator(args)