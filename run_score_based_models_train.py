import os
import yaml
import argparse
from omegaconf import OmegaConf

from src import (get_standard_sde, score_model_simple_trainer, get_standard_score, 
		 get_standard_train_dataset)

parser = argparse.ArgumentParser(description='training')
parser.add_argument('--sde', default='vesde', choices=['vpsde', 'vesde', 'ddpm'])
parser.add_argument('--base_path', default='/localdata/AlexanderDenker/score_based_baseline')
parser.add_argument('--train_model_on', default='ellipses', help='training datasets', choices=['lodopab', 'lodopab_dival', 'ellipses'])
parser.add_argument("--model_type", default="openai_unet", choices=["openai_unet", "dds_unet"])

def coordinator(args):

	# different configs formats for different models, ugly but works for now 
	if args.model_type == "openai_unet":
		if args.train_model_on == 'ellipses': 
			from configs.disk_ellipses_configs import get_config
			config = get_config(args)
		elif args.train_model_on == 'lodopab': 
			from configs.lodopab_configs import get_config
			config = get_config(args)
		elif args.train_model_on == 'lodopab_dival':
			from configs.lodopab_challenge_configs import get_config
			config = get_config(args)
		else: 
			raise NotImplementedError
	elif args.model_type == "dds_unet":
		if args.train_model_on == 'ellipses':
			with open("/home/adenker/projects/diffusion_models_dev_project/ellipses_configs/ddpm/Ellipse256.yml", 'r') as stream:
				config = yaml.load(stream, Loader=yaml.UnsafeLoader)
				config = OmegaConf.create(config)
		else:
			raise NotImplementedError
	print(config)
	sde = get_standard_sde(config=config)
	score = get_standard_score(config=config, sde=sde, use_ema=False, load_model=False, model_type=args.model_type)

	print("Number of parameters: ", sum([p.numel() for p in score.parameters()]))

	base_path = args.base_path
	if config.data.name == 'LoDoPabCT':
		log_dir = os.path.join(base_path, 'LoDoPabCT')
	elif config.data.name == 'DiskDistributedEllipsesDataset':
		log_dir = os.path.join(base_path, 'DiskEllipses')
	else:
		raise NotImplementedError

	log_dir = os.path.join(log_dir, args.model_type)

	log_dir = os.path.join(log_dir, config.sde.type)
	if not os.path.exists(log_dir):
		os.makedirs(log_dir)

	found_version = False 
	version_num = 1
	while not found_version:
		if os.path.isdir(os.path.join(log_dir, "version_{:02d}".format(version_num))):
			version_num += 1
		else:
			found_version = True 

	log_dir = os.path.join(log_dir, "version_{:02d}".format(version_num))
	print("save model to ", log_dir)
	os.makedirs(log_dir)

	with open(os.path.join(log_dir,'report.yaml'), 'w') as file:
		try:
			yaml.dump(config, file)
		except AttributeError:
			yaml.dump(OmegaConf.to_container(config, resolve=True), file)

	train_dl = get_standard_train_dataset(config)
	score_model_simple_trainer(
			score=score.to(config.device),
			sde=sde,
			train_dl=train_dl,
			optim_kwargs={
					'epochs': config.training.epochs,
					'lr': float(config.training.lr),
					'ema_warm_start_steps': config.training.ema_warm_start_steps,
					'log_freq': config.training.log_freq,
					'ema_decay': config.training.ema_decay, 
					'save_model_every_n_epoch': config.training.save_model_every_n_epoch
				},
			val_kwargs={
					'batch_size': config.validation.batch_size,
					'num_steps': config.validation.num_steps,
					'snr': config.validation.snr,
					'eps': config.validation.eps,
					'sample_freq' : config.validation.sample_freq
				},
		device=config.device,
		log_dir=log_dir
		)

if __name__ == '__main__':
	args = parser.parse_args()
	coordinator(args)
