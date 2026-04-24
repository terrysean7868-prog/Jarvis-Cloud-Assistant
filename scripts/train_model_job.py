import os
import sys
import logging
from pathlib import Path

try:
    from datasets import load_dataset, concatenate_datasets, Dataset
    from transformers import AutoModelForCausalLM, AutoTokenizer, TrainingArguments
    from peft import LoraConfig, get_peft_model
    from trl import SFTTrainer
except ImportError:
    print("⚠️ Required libraries missing for local training. Please run:")
    print("pip install transformers datasets peft trl torch bitsandbytes")
    sys.exit(1)

def run_training():
    print("🚀 Starting AI Assistant Auto-Training Pipeline")
    # Using a 4-bit quantized base model to drastically reduce memory requirements
    model_id = "unsloth/llama-3-8b-Instruct-bnb-4bit" 
    
    print("📦 Downloading human-like dataset from HuggingFace...")
    # Use daily_dialog to give the assistant a more natural, human conversational flow
    try:
        hf_ds = load_dataset("daily_dialog", split="train[:1000]") 
        
        def format_hf(example):
            if len(example["dialog"]) >= 2:
                text = "User: " + example["dialog"][0] + "\nAssistant: " + example["dialog"][1]
            else:
                text = "User: " + example["dialog"][0] + "\nAssistant: I hear you."
            return {"text": text}
        
        hf_ds = hf_ds.map(format_hf, remove_columns=hf_ds.column_names)
    except Exception as e:
        print(f"⚠️ Failed to load HuggingFace dataset: {e}")
        hf_ds = None
    
    # Merge with the local dataset containing voice logs and task logs
    local_ds_path = Path(__file__).resolve().parents[1] / "data" / "ai_training" / "datasets" / "conversation_dataset.jsonl"
    
    if local_ds_path.exists():
        print("🔗 Merging local voice & chat logs...")
        local_ds = load_dataset("json", data_files=str(local_ds_path), split="train")
        
        def format_local(example):
            return {"text": f"User: {example.get('input', '')}\nAssistant: {example.get('expected_output', '')}"}
            
        local_ds = local_ds.map(format_local, remove_columns=local_ds.column_names)
        
        if hf_ds is not None:
            train_ds = concatenate_datasets([hf_ds, local_ds])
        else:
            train_ds = local_ds
    else:
        print("⚠️ Local dataset not found. Training on HuggingFace dataset only.")
        train_ds = hf_ds

    if train_ds is None:
        print("❌ No data available for training. Exiting.")
        sys.exit(1)

    print("🧠 Loading model and tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(model_id)
    tokenizer.pad_token = tokenizer.eos_token
    
    model = AutoModelForCausalLM.from_pretrained(
        model_id, 
        device_map="auto"
    )
    
    # Low-Rank Adaptation (LoRA) allows us to train rapidly without massive compute
    peft_config = LoraConfig(
        r=16,
        lora_alpha=32,
        target_modules=["q_proj", "v_proj"],
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM"
    )
    
    model = get_peft_model(model, peft_config)
    
    # Define training arguments specifically optimized for periodic background updates
    args = TrainingArguments(
        output_dir="./custom_jarvis_model",
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,
        warmup_steps=5,
        max_steps=50, # Keep the steps short so the 24h cron job completes quickly
        learning_rate=2e-4,
        logging_steps=10,
        optim="adamw_8bit",
        save_strategy="no",
        report_to="none"
    )
    
    trainer = SFTTrainer(
        model=model,
        train_dataset=train_ds,
        dataset_text_field="text",
        max_seq_length=512,
        args=args,
    )
    
    print("🔥 Starting fine-tuning...")
    trainer.train()
    
    print("💾 Saving custom trained model...")
    save_path = Path(__file__).resolve().parents[1] / "models" / "jarvis_custom"
    trainer.model.save_pretrained(str(save_path))
    tokenizer.save_pretrained(str(save_path))
    print(f"✅ Training complete. Model is saved to {save_path} and ready for inference.")

if __name__ == "__main__":
    run_training()
