"""
Learning module for Jarvis that handles pattern recognition, knowledge acquisition,
and memory management in learning mode.
"""

import asyncio
from datetime import datetime
from typing import Dict, List, Any, Optional
from utils.db import db
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import DBSCAN
from collections import defaultdict

class KnowledgeGraph:
    def __init__(self):
        self.nodes = {}
        self.edges = defaultdict(list)
        self.last_update = datetime.utcnow()
    
    def add_node(self, node_id: str, data: Dict[str, Any]):
        """Add or update a node in the knowledge graph"""
        self.nodes[node_id] = {
            'data': data,
            'timestamp': datetime.utcnow(),
            'connections': []
        }
    
    def add_edge(self, from_node: str, to_node: str, relationship: str):
        """Add a relationship between nodes"""
        if from_node in self.nodes and to_node in self.nodes:
            self.edges[from_node].append({
                'to': to_node,
                'relationship': relationship,
                'timestamp': datetime.utcnow()
            })

class PatternRecognizer:
    def __init__(self):
        self.vectorizer = TfidfVectorizer(
            max_features=1000,
            stop_words='english'
        )
        self.clustering = DBSCAN(
            eps=0.3,
            min_samples=2,
            metric='cosine'
        )
        self.patterns = []
    
    def find_patterns(self, texts: List[str]) -> List[Dict[str, Any]]:
        """Identify patterns in a collection of texts"""
        if not texts:
            return []
            
        # Vectorize texts
        try:
            vectors = self.vectorizer.fit_transform(texts)
            
            # Perform clustering
            clusters = self.clustering.fit_predict(vectors)
            
            # Extract patterns
            patterns = []
            for cluster_id in set(clusters):
                if cluster_id == -1:  # Noise points
                    continue
                    
                cluster_texts = [
                    texts[i] for i in range(len(texts))
                    if clusters[i] == cluster_id
                ]
                
                pattern = {
                    'cluster_id': int(cluster_id),
                    'texts': cluster_texts,
                    'size': len(cluster_texts),
                    'timestamp': datetime.utcnow()
                }
                patterns.append(pattern)
            
            self.patterns.extend(patterns)
            return patterns
            
        except Exception as e:
            db.save_system_event(
                event_type='pattern_recognition_error',
                description=str(e),
                status='error'
            )
            return []

class LearningModule:
    def __init__(self):
        self.knowledge_graph = KnowledgeGraph()
        self.pattern_recognizer = PatternRecognizer()
        self.short_term_memory = []
        self.learning_rate = 0.1
    
    async def process_interaction(self, interaction: Dict[str, Any]) -> Dict[str, Any]:
        """Process a new interaction and update knowledge"""
        try:
            # Add to short-term memory
            self.short_term_memory.append({
                'data': interaction,
                'timestamp': datetime.utcnow()
            })
            
            # Extract text content
            text_content = interaction.get('text', '')
            
            # Add to knowledge graph
            node_id = f"interaction_{len(self.knowledge_graph.nodes)}"
            self.knowledge_graph.add_node(node_id, interaction)
            
            # Find patterns in recent interactions
            recent_texts = [
                m['data'].get('text', '')
                for m in self.short_term_memory[-10:]
                if m['data'].get('text')
            ]
            
            patterns = self.pattern_recognizer.find_patterns(recent_texts)
            
            # Update database
            db.save_system_event(
                event_type='learning_interaction',
                description='Processed new interaction',
                status='success',
                details={
                    'patterns_found': len(patterns),
                    'knowledge_nodes': len(self.knowledge_graph.nodes)
                }
            )
            
            return {
                'status': 'success',
                'patterns': patterns,
                'node_id': node_id
            }
            
        except Exception as e:
            db.save_system_event(
                event_type='learning_error',
                description=str(e),
                status='error'
            )
            return {
                'status': 'error',
                'error': str(e)
            }
    
    def get_learning_status(self) -> Dict[str, Any]:
        """Get current learning status and statistics"""
        return {
            'short_term_memory_size': len(self.short_term_memory),
            'knowledge_nodes': len(self.knowledge_graph.nodes),
            'patterns_found': len(self.pattern_recognizer.patterns),
            'learning_rate': self.learning_rate,
            'last_update': self.knowledge_graph.last_update.isoformat()
        }
    
    async def consolidate_memory(self):
        """Move short-term memory patterns to long-term storage"""
        try:
            # Process all short-term memory items
            texts = [
                m['data'].get('text', '')
                for m in self.short_term_memory
                if m['data'].get('text')
            ]
            
            patterns = self.pattern_recognizer.find_patterns(texts)
            
            # Store patterns in database
            for pattern in patterns:
                db.patterns.insert_one({
                    'texts': pattern['texts'],
                    'cluster_id': pattern['cluster_id'],
                    'timestamp': pattern['timestamp']
                })
            
            # Clear short-term memory
            self.short_term_memory = []
            
            db.save_system_event(
                event_type='memory_consolidation',
                description='Consolidated memory patterns',
                status='success',
                details={'patterns_stored': len(patterns)}
            )
            
        except Exception as e:
            db.save_system_event(
                event_type='memory_consolidation_error',
                description=str(e),
                status='error'
            )

# Create singleton instance
learning = LearningModule()