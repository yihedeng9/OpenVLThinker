import os
import logging
from dataclasses import dataclass
from typing import List, Dict, Optional, Union
import torch
from datasets import load_dataset
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration
from qwen_vl_utils import process_vision_info
import json
from tqdm import tqdm
from PIL import Image
import requests
from io import BytesIO
import argparse
from mathruler.grader import grade_answer
from pathlib import Path
from enum import Enum

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('evaluation.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class DatasetType(Enum):
    MATHVISTA = "mathvista"
    MATHVERSE = "mathverse"
    MATHVISION = "mathvision"

@dataclass
class DatasetConfig:
    name: str
    split: str
    image_field: str
    instruction_field: str
    response_field: str
    choices_field: Optional[str] = None
    options_field: Optional[str] = None

@dataclass
class ModelConfig:
    model_name: str
    processor_name: str
    max_new_tokens: int = 2048
    top_p: float = 0.001
    top_k: int = 1
    temperature: float = 0.01
    repetition_penalty: float = 1.0

class ImageProcessor:
    def __init__(self, model_config: ModelConfig, device: str):
        self.device = device
        self.model_config = model_config
        self.model = self._load_model()
        self.processor = self._load_processor()

    def _load_model(self) -> Qwen2_5_VLForConditionalGeneration:
        try:
            return Qwen2_5_VLForConditionalGeneration.from_pretrained(
                self.model_config.model_name,
                torch_dtype=torch.bfloat16,
                attn_implementation="flash_attention_2",
                device_map=self.device
            )
        except Exception as e:
            logger.error(f"Failed to load model: {str(e)}")
            raise

    def _load_processor(self) -> AutoProcessor:
        try:
            return AutoProcessor.from_pretrained(self.model_config.processor_name)
        except Exception as e:
            logger.error(f"Failed to load processor: {str(e)}")
            raise

    def generate_answer(self, image_url: str, instruction: str) -> Optional[str]:
        try:
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "image": image_url},
                        {"type": "text", "text": instruction},
                    ],
                }
            ]
            
            text = self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            image_inputs, video_inputs = process_vision_info(messages)
            inputs = self.processor(
                text=[text],
                images=image_inputs,
                videos=video_inputs,
                padding=True,
                return_tensors="pt",
            ).to(self.device)
            
            generated_ids = self.model.generate(
                **inputs,
                do_sample=True,
                max_new_tokens=self.model_config.max_new_tokens,
                top_p=self.model_config.top_p,
                top_k=self.model_config.top_k,
                temperature=self.model_config.temperature,
                repetition_penalty=self.model_config.repetition_penalty,
            )
            
            generated_ids_trimmed = [
                out_ids[len(in_ids):] for in_ids, out_ids in zip(inputs.input_ids, generated_ids)
            ]
            return self.processor.batch_decode(
                generated_ids_trimmed,
                skip_special_tokens=True,
                clean_up_tokenization_spaces=False
            )[0]
        
        except Exception as e:
            logger.error(f"Error generating answer: {str(e)}")
            return None

def get_dataset_config(dataset_type: DatasetType) -> DatasetConfig:
    configs = {
        DatasetType.MATHVISTA: DatasetConfig(
            name="AI4Math/MathVista",
            split="testmini",
            image_field="decoded_image",
            instruction_field="query",
            response_field="answer",
            choices_field="choices"
        ),
        DatasetType.MATHVERSE: DatasetConfig(
            name="AI4Math/MathVerse",
            split="testmini",
            image_field="image",
            instruction_field="query_cot",
            response_field="answer"
        ),
        DatasetType.MATHVISION: DatasetConfig(
            name="MathLLMs/MathVision",
            split="testmini",
            image_field="decoded_image",
            instruction_field="question",
            response_field="answer",
            options_field="options"
        )
    }
    return configs[dataset_type]

def load_image_dataset(dataset_config: DatasetConfig) -> List[Dict]:
    try:
        data = load_dataset(dataset_config.name, split=dataset_config.split)
        items = []
        for item in data:
            dataset_item = {
                'image_url': item[dataset_config.image_field],
                'instruction': item.get(dataset_config.instruction_field, ''),
                'response': item.get(dataset_config.response_field, ''),
            }
            if dataset_config.choices_field:
                dataset_item['choices'] = item.get(dataset_config.choices_field)
            if dataset_config.options_field:
                dataset_item['options'] = item.get(dataset_config.options_field, [])
            items.append(dataset_item)
        return items
    except Exception as e:
        logger.error(f"Failed to load dataset: {str(e)}")
        raise

def save_descriptions(descriptions: List[Dict], output_file: str) -> None:
    try:
        output_path = Path(output_file)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w') as f:
            json.dump(descriptions, f, indent=2)
        logger.info(f"Saved {len(descriptions)} descriptions to {output_file}")
    except Exception as e:
        logger.error(f"Failed to save descriptions: {str(e)}")
        raise

def process_response(response: str, choices: Optional[List[str]], options: Optional[List[str]] = None) -> str:
    if choices is not None:
        try:
            response_index = choices.index(response)
            return ['A', 'B', 'C', 'D', 'E', 'F', 'G'][response_index]
        except ValueError:
            pass
    if options is not None and len(options) > 0:
        try:
            response_index = options.index(response)
            return ['A', 'B', 'C', 'D', 'E', 'F', 'G'][response_index]
        except ValueError:
            pass
    return response

def format_instruction(instruction: str, options: Optional[List[str]] = None) -> str:
    if options and len(options) > 0:
        prompt_hint = "Hint: Please answer the question and provide the correct option letter, e.g., A, B, C, D, E, at the end."
        choice_list = "\n".join(f"({chr(65+i)}) {opt}" for i, opt in enumerate(options))
        return f"{prompt_hint}\nQuestion: {instruction}\nChoices:\n{choice_list}"
    else:
        prompt_hint = "Hint: Please answer the question requiring an answer."
        return f"{prompt_hint}\nQuestion: {instruction}"

def main():
    parser = argparse.ArgumentParser(description='Evaluate model on various math datasets')
    parser.add_argument('--cuda', type=int, default=0, help='CUDA device number to use')
    parser.add_argument('--batch_size', type=int, default=1, help='Batch size for processing')
    parser.add_argument('--dataset', type=str, choices=['mathvista', 'mathverse', 'mathvision'],
                      default='mathvista', help='Dataset to evaluate on')
    parser.add_argument('--model_path', type=str, help='Path to the model', default="ydeng9/OpenVLThinker-7B")
    args = parser.parse_args()
    
    device = f"cuda:{args.cuda}" if torch.cuda.is_available() else "cpu"
    logger.info(f"Using device: {device}")

    # Configuration
    dataset_type = DatasetType(args.dataset)
    dataset_config = get_dataset_config(dataset_type)
    model_config = ModelConfig(
        model_name=args.model_path,
        processor_name="Qwen/Qwen2.5-VL-7B-Instruct"
    )
    
    output_file = f"./evaluation/outputs/{dataset_type.value}_{model_config.model_name.split('/')[-1]}.json"
    
    # Initialize processor and model
    logger.info(f"Loading model {model_config.model_name}")
    processor = ImageProcessor(model_config, device)
    
    # Load dataset
    logger.info(f"Loading dataset {dataset_config.name}")
    data = load_image_dataset(dataset_config)
    
    descriptions = []
    correct = 0

    # Process each image
    for i, item in tqdm(enumerate(data), total=len(data), desc="Processing images"):
        correct_flag = 0
        if dataset_type == DatasetType.MATHVISION:
            formatted_instruction = format_instruction(item['instruction'], item.get('options'))
        else:
            formatted_instruction = item['instruction']
        answer = processor.generate_answer(item['image_url'], formatted_instruction)
        reasoning = answer
        
        if answer and "</answer>" in answer:
            answer = answer.split("<answer>")[-1].split("</answer>")[0].strip()
            processed_response = process_response(
                item['response'],
                item.get('choices'),
                item.get('options')
            )
            
            if processed_response.lower() == answer.lower() or grade_answer(processed_response, answer):
                correct += 1
                correct_flag = 1
        else:
            answer = "Failed to extract."
            logger.warning(f"Failed to extract answer for question {i}")

        description = {
            'instruction': item['instruction'],
            'response': item['response'],
            'reasoning': reasoning,
            'answer': answer,
            'correct': correct_flag
        }
        descriptions.append(description)
        
        # Save periodically
        if (i + 1) % 10 == 0:
            save_descriptions(descriptions, output_file)
    
    # Final save
    save_descriptions(descriptions, output_file)
    accuracy = correct / len(data)
    logger.info(f"Completed! Final accuracy: {accuracy:.4f}")

if __name__ == "__main__":
    main() 
