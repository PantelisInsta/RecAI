# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import re
import json
import random
from loguru import logger
from copy import deepcopy

from llm4crs.utils import num_tokens_from_string, cut_list
from llm4crs.corpus import BaseGallery
from llm4crs.utils.sql import extract_columns_from_where



class QueryTool:
    '''
    Used to search information from the item corpus. Receives a SQL command as input and returns the search result. 
    If need be, the SQL command is rewritten to match the item corpus.
    '''

    def __init__(self, name: str, desc: str, item_corpus: BaseGallery, buffer, result_max_token: int=512) -> None:
        self.item_corpus = item_corpus
        self.name = name
        self.desc = desc
        self.buffer = buffer
        self.result_max_token = result_max_token
        self._max_record_num = self.result_max_token // 5   # each record at least 5 tokens. If too many records, sample randomly


    def run(self, inputs: str) -> str:
        """Search information from the item corpus. Truncate the result if it exceeds the token limit."""

        logger.debug(f"\nSQL from AGI: {inputs}")
        info = ""
        output = "can not seach related information."
        try:
            inputs = self.rewrite_sql(inputs)
            logger.debug(f"Rewrite SQL: {inputs}")
            info += f"{self.name}: The input SQL is rewritten as {inputs} because some {self.item_corpus.categorical_col_values.keys()} are not existing. \n"
        except:
            info += f"{self.name}: some thing went wrong in execution, the tool is broken for current input. \n"
            return info
        
        try:
            res = self.item_corpus(inputs, return_id_only=False)
            try:
                res = res.to_dict('records')   # list of dict
            except:
                pass
            _any_cut = False

            # If the result is too long, sample results randomly
            if len(res) > self._max_record_num:
                _any_cut = True
                res = random.sample(res, k=self._max_record_num)

            # If results exceed token limit, shorten the result by cutting off the last words of each record
            if num_tokens_from_string(json.dumps(res)) > self.result_max_token:
                token_limit_per_record = self.result_max_token // (len(res)+1)

                # shorten each record in the result list
                cut_res = [None] * len(res)
                for i, record in enumerate(res):
                    overflow_token = num_tokens_from_string(json.dumps(record)) - token_limit_per_record
                    _cut_last = True
                    res_record = deepcopy(record)
                    
                    # if not cut last time, the loop would end due to the cut operation in the loop would not shorten the res anymore.
                    while (overflow_token > 0) and _cut_last:  
                        key2token = {}
                        all_token = 0
                        for k, v in res_record.items():
                            key2token[k] = num_tokens_from_string(str(v))
                            all_token += key2token[k]
                            res_record[k] = str(v)

                        cut_off_token = {k: (overflow_token * v // all_token) for k, v in key2token.items()}
                        for k, v in res_record.items():
                            words = v.split(" ")
                            cut_word_cnt = min(len(words) * cut_off_token[k] // key2token[k], len(words)-3)
                            if (cut_word_cnt >= 1) and (len(words) > 10):
                                words = words[:-cut_word_cnt]
                                _suffix = "..."
                                _cut_last = True
                            elif (cut_word_cnt >= 1) and (len(words) > 5):
                                words = words[:-1]
                                _suffix = "..."
                                _cut_last = True
                            else:
                                _cut_last = False
                                _suffix = ""
                            res_record[k] = ' '.join(words) + _suffix
                        overflow_token = num_tokens_from_string(json.dumps(res_record)) - token_limit_per_record
                    cut_res[i] = res_record

                # END FOR LOOP
                if _any_cut:
                    info += f"{self.name}: The search result is too long, some are omitted. \n"

                # double check the token limit and shorten the result list
                if num_tokens_from_string(json.dumps(cut_res)) > self.result_max_token:
                    cut_res = cut_list(cut_res, self.result_max_token)
                    
                output = json.dumps(cut_res)
            
            else:
                output = json.dumps(res)
            
            info += f"{self.name} search result: {output}"

        except Exception as e:
            logger.debug(e)
            info += f"{self.name}: some thing went wrong in execution, the tool is broken for current input. \n"
        
        # Track tool usage
        self.buffer.track(self.name, inputs, "Some item information.")
        logger.debug(info)

        return output

    def rewrite_sql(self, sql: str) -> str:
        """
        Rewrite SQL command using fuzzy search. Looks into the SQL command and 
        replaces the column names and categorical values with the closest match
        from the item corpus.
        """
        
        sql = re.sub(r'\bFROM\s+(\w+)\s+WHERE', f'FROM {self.item_corpus.name} WHERE', sql, flags=re.IGNORECASE)
        
        # grounding cols
        cols = extract_columns_from_where(sql)
        existing_cols = set(self.item_corpus.column_meaning.keys())
        col_replace_dict = {}
        for col in cols:
            if col not in existing_cols:
                mapped_col = self.item_corpus.fuzzy_match(col, 'sql_cols')
                col_replace_dict[col] = f"{mapped_col}"
        for k, v in col_replace_dict.items():
            sql = sql.replace(k, v)

        # grounding categorical values
        pattern = r"([a-zA-Z0-9_]+) (?:NOT )?LIKE '\%([^\%]+)\%'" 
        res = re.findall(pattern, sql)
        replace_dict = {}
        for col, value in res:
            if col not in self.item_corpus.fuzzy_engine:
                continue
            replace_value = str(self.item_corpus.fuzzy_match(value, col))
            replace_value = replace_value.replace("'", "''")    # escaping string for sqlite
            replace_dict[f"%{value}%"] = f"%{replace_value}%"

        for k, v in replace_dict.items():
            sql = sql.replace(k, v)
            
        return sql