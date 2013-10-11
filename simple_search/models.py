import logging
import shlex

from django.db import models
from google.appengine.ext import db

"""
    REMAINING TO DO!

    1. Partial matches. These should be recorded as new Index instances, with an FK to the full term being indexes.
       Partials should only be recorded for between 4, and len(original_term) - 1 characters. Partial matches should be much more highly scored,
       the lower the match, the more the score should be
    2. Cross-join indexing  e.g. book__title on an Author.
    3. Field matches. e.g "id:1234 field1:banana". This should match any other words using indexes, but only return matches that match the field lookups
"""

def index_instance(instance, fields_to_index):
    unindex_instance(instance)

    for field in fields_to_index:
        text = getattr(instance, field, None)
        if text:
            text = text.lower() #Normalize

            words = text.split(" ") #Split on whitespace

            #Build up combinations of adjacent words
            for i in xrange(0, len(words)):
                for j in xrange(1, 5):
                    term_words = words[i:i+j]

                    if len(term_words) != j:
                        break

                    term = " ".join(term_words)

                    if not term.strip(): continue

                    logging.info("Indexing: '%s'", term)
                    index, created = Index.objects.get_or_create(
                        iexact=term,
                        instance_db_table=instance._meta.db_table,
                        instance_pk=instance.pk
                    )
                    def txn(term, index):
                        index = Index.objects.get(pk=index.pk)
                        index.occurances += text.count(term)
                        index.save()

                        counter, created = GlobalOccuranceCount.objects.get_or_create(pk=term)
                        counter.count += text.count(term)
                        counter.save()

                    db.run_in_transaction_options(db.create_transaction_options(xg=True), txn, term, index)

def unindex_instance(instance):
    indexes = Index.objects.filter(instance_db_table=instance._meta.db_table, instance_pk=instance.pk).all()
    for index in indexes:
        count = GlobalOccuranceCount.objects.get(pk=index.iexact)
        count.count -= index.occurances
        count.save()
        index.delete()

def parse_terms(search_string):
    return shlex.split(search_string.lower())

def search(model_class, search_string, per_page=50, current_page=1, total_pages=10):
    terms = parse_terms(search_string)

    #Get all matching terms
    matching_terms = dict(GlobalOccuranceCount.objects.filter(pk__in=terms).values_list('pk', 'count'))
    matches = Index.objects.filter(iexact__in=terms, instance_db_table=model_class._meta.db_table).all()

    instance_weights = {}

    for match in matches:
        instance_weights.setdefault(match.instance_pk, []).append(matching_terms[match.iexact])

    final_weights = []
    for k, v in instance_weights.items():
        """
            This is where we rank the results. Lower scores are better. Scores are based
            on the commonality of the word. More matches are rewarded, but not too much so
            that rarer terms still have a chance.

            Examples for n matches:

            1 = 1 + (0 * 0.5) = 1    -> scores / 1
            2 = 2 + (1 * 0.5) = 2.5  -> scores / 2.5 (rather than 2)
            3 = 3 + (2 * 0.5) = 4    -> scores / 4 (rather than 3)
        """

        n = float(len(v))
        final_weights.append((sum(v) / (n + ((n-1) * 0.5)), k))

    final_weights.sort()

    #Restrict to the max possible
    final_weights = final_weights[:total_pages*per_page]

    #Restrict to the page
    offset = ((current_page - 1) * per_page)
    final_weights = final_weights[offset:offset + per_page]

    order = {}
    for index, (score, pk) in enumerate(final_weights):
        order[pk] = index

    sorted_results = [None] * len(order.keys())
    results = model_class.objects.filter(pk__in=order.keys())
    for result in results:
        position = order[result.pk]
        sorted_results[position] = result

    return results

class GlobalOccuranceCount(models.Model):
    id = models.CharField(max_length=1024, primary_key=True)
    count = models.PositiveIntegerField(default=0)

class Index(models.Model):
    iexact = models.CharField(max_length=1024)
    instance_db_table = models.CharField(max_length=1024)
    instance_pk = models.PositiveIntegerField(default=0)
    occurances = models.PositiveIntegerField(default=0)

from django.dispatch import receiver
from django.db.models.signals import post_save, pre_delete

@receiver(post_save)
def post_save(sender, instance, created, raw, using, *args, **kwargs):
    if getattr(instance, "Search", None):
        fields_to_index = getattr(instance.Search, "fields", [])
        if fields_to_index:
            index_instance(instance, fields_to_index)

@receiver(pre_delete)
def pre_delete(sender, instance, using, *args, **kwarg):
    if getattr(instance, "Search", None):
        unindex_instance(instance)