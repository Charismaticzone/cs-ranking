import hashlib
import json
import logging
import os
from abc import ABCMeta
from datetime import timedelta, datetime

import psycopg2
from psycopg2.extras import DictCursor

from csrank.util import get_duration_seconds


class DBConnector(metaclass=ABCMeta):

    def __init__(self, config_file_path, is_gpu=False, schema='master', **kwargs):
        self.logger = logging.getLogger('DBConnector')
        self.is_gpu = is_gpu
        self.schema = schema
        self.job_description = None
        self.connection = None
        self.cursor_db = None
        if os.path.isfile(config_file_path):
            config_file = open(config_file_path, "r")
            config = config_file.read().replace('\n', '')
            self.logger.info("Config {}".format(config))
            self.connect_params = json.loads(config)
            self.logger.info("Connection Successful")
        else:
            raise ValueError('File does not exist for the configuration of the database')

    def init_connection(self, cursor_factory=DictCursor):
        self.connection = psycopg2.connect(**self.connect_params)
        if cursor_factory is None:
            self.cursor_db = self.connection.cursor()
        else:
            self.cursor_db = self.connection.cursor(cursor_factory=cursor_factory)

    def close_connection(self):
        self.connection.commit()
        self.connection.close()

    def create_hash_value(self):
        keys = ['learner', 'dataset_params', 'fit_params', 'learner_params', 'hp_ranges', 'hp_fit_params',
                'inner_folds', 'validation_loss', 'fold_id']
        hash_string = ""
        for k, v in self.job_description.items():
            if k in keys:
                hash_string = hash_string + str(k) + ':' + str(v)
        hash_object = hashlib.sha1(hash_string.encode())
        hex_dig = hash_object.hexdigest()
        return str(hex_dig)

    def add_jobs_in_avail_which_failed(self):
        self.init_connection()
        avail_jobs = "{}.avail_jobs".format(self.schema)
        running_jobs = "{}.running_jobs".format(self.schema)
        select_job = """SELECT * FROM {0} row WHERE EXISTS(SELECT job_id FROM {1} r WHERE r.interrupted = FALSE AND r.finished = FALSE AND r.job_id = row.job_id)""".format(
            avail_jobs, running_jobs)
        self.cursor_db.execute(select_job)
        all_jobs = self.cursor_db.fetchall()
        self.close_connection()
        for job in all_jobs:
            date_time = job['job_allocated_time']
            duration = get_duration_seconds(job['duration'])
            new_date = date_time + timedelta(seconds=duration)
            if new_date < datetime.now():
                job_id = int(job['job_id'])
                print("Duration for the Job {} expired so marking it as failed".format(job_id))
                error_message = "exception{}".format("InterruptedDueToSomeError")
                self.append_error_string_in_running_job(job_id=job_id, error_message=error_message)

    def fetch_job_arguments(self, cluster_id):
        self.add_jobs_in_avail_which_failed()
        self.init_connection()
        avail_jobs = "{}.avail_jobs".format(self.schema)
        running_jobs = "{}.running_jobs".format(self.schema)
        select_job = """SELECT job_id FROM {0} row WHERE (is_gpu = {2})AND NOT EXISTS(SELECT job_id FROM {1} r WHERE r.interrupted = FALSE AND r.job_id = row.job_id)""".format(
            avail_jobs, running_jobs, self.is_gpu)

        self.cursor_db.execute(select_job)
        job_ids = [j for i in self.cursor_db.fetchall() for j in i]
        while self.job_description is None:
            try:
                job_id = job_ids[0]
                print("Job selected : {}".format(job_id))
                select_job = "SELECT * FROM {0} WHERE {0}.job_id = {1}".format(avail_jobs, job_id)
                self.cursor_db.execute(select_job)
                self.job_description = self.cursor_db.fetchone()

                hash_value = self.create_hash_value()
                self.job_description["hash_value"] = hash_value
                self.close_connection()
            except psycopg2.IntegrityError as e:
                print("IntegrityError for the job {}, already assigned to another node error {}".format(job_id, str(e)))
                job_ids.remove(job_id)
            except (ValueError, IndexError) as e:
                print("Error as the all jobs are already assigned to another nodes {}".format(str(e)))
                break
        if self.job_description is not None:
            try:
                self.init_connection(cursor_factory=None)
                start = datetime.now()
                update_job = """UPDATE {} set hash_value = %s, job_allocated_time = %s WHERE job_id = %s""".format(
                    avail_jobs)
                self.cursor_db.execute(update_job, (hash_value, start, job_id))
                select_job = """SELECT count(*) FROM {0} WHERE {0}.job_id = {1}""".format(running_jobs, job_id)
                self.cursor_db.execute(select_job)
                count_ = self.cursor_db.fetchone()[0]
                if count_ == 0:
                    insert_job = """INSERT INTO {0} (job_id, cluster_id ,finished, interrupted) VALUES ({1}, {2},FALSE, FALSE)""".format(
                        running_jobs, job_id, cluster_id)
                    self.cursor_db.execute(insert_job)
                    if self.cursor_db.rowcount == 1:
                        print("The job {} is inserted".format(job_id))
                else:
                    update_job = """UPDATE {} set cluster_id = %s, interrupted = %s WHERE job_id = %s""".format(
                        running_jobs)
                    self.cursor_db.execute(update_job, (cluster_id, 'FALSE', job_id))
                    if self.cursor_db.rowcount == 1:
                        print("The job {} is updated".format(job_id))

                self.close_connection()
            except (psycopg2.IntegrityError, psycopg2.DatabaseError) as e:
                print("IntegrityError for the job {} error {}".format(job_id, str(e)))
                self.job_description = None

    def mark_running_job_finished(self, job_id, **kwargs):
        self.init_connection()
        running_jobs = "{}.running_jobs".format(self.schema)
        update_job = "UPDATE {0} set finished = TRUE, interrupted = FALSE where job_id = {1}".format(running_jobs,
            job_id)
        self.cursor_db.execute(update_job)
        if self.cursor_db.rowcount == 1:
            self.logger.info("The job {} is finished".format(job_id))
        self.close_connection()

    def insert_results(self, experiment_schema, experiment_table, results, **kwargs):
        self.init_connection(cursor_factory=None)
        results_table = "{}.{}".format(experiment_schema, experiment_table)
        columns = ', '.join(list(results.keys()))
        values_str = ', '.join(list(results.values()))

        self.cursor_db.execute("select to_regclass(%s)", [results_table])
        is_table_exist = bool(self.cursor_db.fetchone()[0])
        if not is_table_exist:
            self.logger.info("Table {} does not exist creating with columns {}".format(results_table, columns))
            create_command = "CREATE TABLE {} (job_id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL)".format(
                results_table)
            self.cursor_db.execute(create_command)
            for column in results.keys():
                if column not in ["job_id", "cluster_id"]:
                    alter_table_command = 'ALTER TABLE %s ADD COLUMN %s double precision' % (results_table, column)
                    self.cursor_db.execute(alter_table_command)
            self.close_connection()
            self.init_connection(cursor_factory=None)

        insert_result = "INSERT INTO {0} ({1}) VALUES ({2})".format(results_table, columns, values_str)
        self.cursor_db.execute(insert_result)
        if self.cursor_db.rowcount == 1:
            self.logger.info("Results inserted for the job {}".format(results['job_id']))
        self.close_connection()

    def append_error_string_in_running_job(self, job_id, error_message, **kwargs):
        self.init_connection(cursor_factory=None)
        running_jobs = "{}.running_jobs".format(self.schema)
        current_message = "SELECT cluster_id, error_history from {0} WHERE {0}.job_id = {1}".format(running_jobs,
            job_id)
        self.cursor_db.execute(current_message)
        cur_message = self.cursor_db.fetchone()
        error_message = "cluster{}".format(cur_message[0]) + error_message
        if cur_message[1] != 'NA':
            error_message = error_message + ';\n' + cur_message[1]
        update_job = "UPDATE {0} SET error_history = %s, interrupted = %s WHERE job_id = %s".format(running_jobs)
        self.cursor_db.execute(update_job, (error_message, True, job_id))
        if self.cursor_db.rowcount == 1:
            self.logger.info("The job {} is interrupted".format(job_id))
        self.close_connection()
