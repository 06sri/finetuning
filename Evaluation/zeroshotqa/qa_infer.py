import os
import argparse
import json
from tqdm import tqdm
import argparse
import torch

import sys

sys.path.insert(0, "/dataset/dcahn/yura/vlm-rlaif/Evaluation")
from llava.constants import IMAGE_TOKEN_INDEX, DEFAULT_IMAGE_TOKEN, DEFAULT_IM_START_TOKEN, DEFAULT_IM_END_TOKEN
from llava.conversation import conv_templates, SeparatorStyle
from llava.model.builder import load_pretrained_model
from llava.utils import disable_torch_init
from llava.mm_utils import process_images, tokenizer_image_token, get_model_name_from_path, KeywordsStoppingCriteria

from PIL import Image
import shutil

import requests
from PIL import Image
from io import BytesIO
from transformers import TextStreamer

import os
import json
from random import randint
from glob import glob


def load_json(fpath):
    with open(fpath, "r") as f:
        return json.load(f)
    
def save_json(data, fpath):
    with open(fpath, "w") as f:
        json.dump(data, f)


def generate_equal_distribution(N, M):
    if M > N:
        raise ValueError("M should be lower than N")
    
    result = []
    count = N // M  # 각 숫자별로 반복되는 횟수
    
    for i in range(1, M+1):
        result.extend([i] * count)
    
    # 나머지 N % M 만큼 남는 부분을 처리
    remainder = N % M
    for i in range(1, remainder+1):
        result.append(i)
    
    return result


def generate_numbers_with_equal_intervals(N, num_frames):
    if N < num_frames:
        raise ValueError("N should be at least 4 for generating 4 numbers with equal intervals.")
    
    # 등간격을 계산
    interval = (N - 1) // (num_frames-1)
    
    # 1부터 N까지의 숫자 중에서 등간격으로 4개의 숫자를 선택
    selected_numbers = [1 + i * interval for i in range(50)]
    
    return selected_numbers



def load_image(image_file):
    if image_file.startswith('http://') or image_file.startswith('https://'):
        response = requests.get(image_file)
        image = Image.open(BytesIO(response.content)).convert('RGB')
    else:
        image = Image.open(image_file).convert('RGB')
    return image



def main(args):
    """
    Run inference on ActivityNet QA DataSet using the Video-ChatGPT model.

    Args:
        args: Command-line arguments.
    """
    LOG_INTERVAL = 100

    # Initialize the model
    disable_torch_init()

    model_name = args.model_path
    if not args.model_base or args.model_base == "none": args.model_base = None
    tokenizer, model, image_processor, context_len = load_pretrained_model(args.model_path, args.model_base, model_name, 
                                                                           args.load_8bit, args.load_4bit)
                                                                        #    args.load_8bit, args.load_4bit, eval_original=True)

    if 'llama-2' in model_name.lower():
        conv_mode = "llava_llama_2"
    elif "v1" in model_name.lower():
        conv_mode = "llava_v1"
    elif "mpt" in model_name.lower():
        conv_mode = "mpt"
    else:
        conv_mode = "llava_v0"

    if args.conv_mode is not None and conv_mode != args.conv_mode:
        print('[WARNING] the auto inferred conversation mode is {}, while `--conv-mode` is {}, using {}'.format(conv_mode, args.conv_mode, args.conv_mode))
    else:
        args.conv_mode = conv_mode
    
    gt_qa = load_json(args.gt_file_qa)

    output_list = []

    output_file_path = os.path.join(args.output_dir, f"{args.output_name}.json")
    if os.path.exists(output_file_path) and args.resume:
        output_list = load_json(output_file_path) # Initialize ouptuts with previously predicted
    else:
        output_list = []
    prev_pred_ids = [d['id'] for d in output_list]
    # Iterate over each sample in the ground truth file
    # index = 0
    cnt = 0
    for idx, sample in tqdm(enumerate(gt_qa), total=len(gt_qa)):
        video_name = sample['video_name']
        id = sample['question_id']
        if id in prev_pred_ids: continue
        
        img_full_path = os.path.join(args.frames_path, 'v_' + video_name)
        # img_full_path = 'playground/data/anet_vidchatgpt_test_1fps_frames/v_' + video_name
        full_vidframes_list = glob(img_full_path + '/*')
        full_vidframes_list.sort()
        
        if len(full_vidframes_list) < args.num_frames: 
            tmp_indexs = generate_equal_distribution(args.num_frames, len(full_vidframes_list))
            tmp_indexs.sort()
            imgs = [full_vidframes_list[x-1] for x in tmp_indexs]
            imgs.sort()
        else:
            imgs = [full_vidframes_list[x-1] for x in generate_numbers_with_equal_intervals(len(full_vidframes_list), args.num_frames)]
            imgs.sort()
            
        # image load
        images = [load_image(img) for img in imgs]
        # Similar operation in model_worker.py
        image_tensor = process_images(images, image_processor, args)
        if args.images:
            image_tensor = image_tensor.to(model.device, dtype=torch.float16)
            image_tensor = image_tensor.unsqueeze(0)  
        elif type(image_tensor) is list:
            image_tensor = [image.to(model.device, dtype=torch.float16) for image in image_tensor]
        else:
            image_tensor = image_tensor.to(model.device, dtype=torch.float16)
            
            
        conv = conv_templates[args.conv_mode].copy()
        
        # question = sample['question'] + answer_prompt
        # answer = gt_answers[idx]['answer']
        question = sample['question']
        answer = sample['answer']
        
        if images is not None:
            # first message
            if model.config.mm_use_im_start_end:
                question = DEFAULT_IM_START_TOKEN + DEFAULT_IMAGE_TOKEN + DEFAULT_IM_END_TOKEN + '\n' + question
            else:
                question = DEFAULT_IMAGE_TOKEN + '\n' + question
            conv.append_message(conv.roles[0], question)
            image = None
        else:
            # later messages
            conv.append_message(conv.roles[0], question)
            # conv.append_message(conv.roles[0], question)
        conv.append_message(conv.roles[1], None)
        prompt = conv.get_prompt()

        sample_set = {'id': id, 'question': question, 'answer': answer}
        
        input_ids = tokenizer_image_token(prompt, tokenizer, IMAGE_TOKEN_INDEX, return_tensors='pt').unsqueeze(0).cuda()
        stop_str = conv.sep if conv.sep_style != SeparatorStyle.TWO else conv.sep2
        keywords = [stop_str]
        stopping_criteria = KeywordsStoppingCriteria(keywords, tokenizer, input_ids)
        streamer = TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
        
        with torch.inference_mode():
            output_ids = model.generate(
                input_ids,
                images=image_tensor,
                do_sample=True,
                temperature=args.temperature,
                max_new_tokens=args.max_new_tokens,
                streamer=streamer,
                use_cache=True,
                stopping_criteria=[stopping_criteria])
            
        outputs = tokenizer.decode(output_ids[0, input_ids.shape[1]:]).strip()
        sample_set['pred'] = outputs
        output_list.append(sample_set)
        cnt += 1
        
        print(sample_set)    
        if args.debug:
            print()
        if cnt % LOG_INTERVAL == 0:
            save_json(output_list, output_file_path)

    save_json(output_list, output_file_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-path", type=str, default="facebook/opt-350m")
    parser.add_argument("--model-base", type=str, default=None)
    parser.add_argument("--images", action="store_true")
    parser.add_argument("--frames_path", required=True)
    parser.add_argument("--num_frames", type=int, required=True)
    parser.add_argument('--output_dir', help='Directory to save the model results JSON.', required=True)
    parser.add_argument('--output_name', help='Name of the file for storing results JSON.', required=True)
    parser.add_argument("--device", type=str, default="cuda")
    parser.add_argument("--conv-mode", type=str, default=None)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--load-8bit", action="store_true")
    parser.add_argument("--load-4bit", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--image-aspect-ratio", type=str, default='pad')
    parser.add_argument('--gt_file_qa', help='Path to the ground truth file containing question.', required=True)
    parser.add_argument("--resume", action="store_true", default=False, help="Whether to resume inference")

    args = parser.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    main(args)
 